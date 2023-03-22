import os
import math
import argparse
from tqdm import tqdm
import torch
import sys
from torch import optim
import json
from torchsummary import summary

import utils.loss
import utils.utils
import utils.datasets
import model.detector


if __name__ == "__main__":
    # 指定训练配置文件
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", type=str, default="",
                        help="Specify training config")
    args = parser.parse_args()
    config_path = args.config
    with open(config_path) as f:
        cfg = json.load(f)

    print("训练配置:")
    print(cfg)

    # 备份代码
    save_dir = cfg["save_dir"]
    os.makedirs(save_dir, exist_ok=True)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    for file in os.listdir(current_dir):
        if ( not file.startswith("_") and "_check" not in file and 
            file not in ["test.onnx"] ):
            srcpath = os.path.join(current_dir, file)
            sys.system("cp -r {} {}".format(srcpath, save_dir))
    weights_dir = os.path.join(save_dir, "weights")
    os.makedirs(weights_dir, exist_ok=True)

    # 数据集加载
    dataset_dir = cfg["dataset_dir"]
    datadump_dir = cfg["datadump_dir"]
    label_dir = os.path.join(dataset_dir, "label")
    train_dir = os.path.join(datadump_dir, "train")
    valid_dir = os.path.join(datadump_dir, "valid")
    train_dataset = utils.datasets.TensorDataset(cfg["cqt"], label_dir, train_dir, cfg["min_duration"], aug = False)
    val_dataset = utils.datasets.TensorDataset(cfg["cqt"], label_dir, valid_dir, cfg["min_duration"], aug = False)

    batch_size = int(cfg["opt"]["batch_size"] / cfg["opt"]["subdivisions"])
    nw = min([os.cpu_count(), batch_size if batch_size > 1 else 0, 8])
    # 训练集
    train_dataloader = torch.utils.data.DataLoader(train_dataset,
                                                   batch_size=batch_size,
                                                   shuffle=True,
                                                   num_workers=nw,
                                                   drop_last=True,
                                                   collate_fn=utils.datasets.collate_fn,
                                                   persistent_workers=True
                                                   )
    #验证集
    val_dataloader = torch.utils.data.DataLoader(val_dataset,
                                                 batch_size=batch_size,
                                                 shuffle=False,
                                                 num_workers=nw,
                                                 drop_last=False,
                                                 collate_fn=utils.datasets.collate_fn,
                                                 persistent_workers=True
                                                 )

    # 指定后端设备CUDA&CPU
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # 判断是否加载预训练模型
    load_param = False
    premodel_path = cfg["m_config"]["pre_weights"]
    if premodel_path != None and os.path.exists(premodel_path):
        load_param = True

    # 初始化模型结构
    model = model.detector.Detector(cfg["cqt"], cfg["m_config"]["width"], cfg["m_config"]["height"], cfg["m_config"]["anchor_num"], load_param).to(device)
    summary(model, input_size=(80448, ))

    # 加载预训练模型参数
    if load_param == True:
        model.load_state_dict(torch.load(premodel_path, map_location=device), strict = False)
        print("Load finefune model param: %s" % premodel_path)
    else:
        print("Initialize weights: model/backbone/backbone.pth")

    # 构建SGD优化器
    optimizer = optim.SGD(params=model.parameters(),
                          lr=cfg["opt"]["learning_rate"],
                          momentum=0.949,
                          weight_decay=0.0005,
                          )

    # 学习率衰减策略
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer,
                                               milestones=cfg["opt"]["steps"],
                                               gamma=0.1)

    print("Starting training for %g epochs..." % cfg["opt"]["epochs"])

    batch_num = 0
    for epoch in range(cfg["opt"]["epochs"]):
        model.train()
        pbar = tqdm(train_dataloader)

        for imgs, targets in pbar:
            # 数据预处理
            imgs = imgs.to(device)
            targets = targets.to(device)

            # 模型推理
            preds = model(imgs)
            cqt = preds[-1]
            preds = preds[: -1]
            # 数据检查
            if cfg["checkdata_dir"]:
                utils.utils.dump_data(cqt.detach().cpu().numpy(), os.path.join(cfg["checkdata_dir"], str(batch_num)), targets.detach().cpu().numpy())
                if batch_num == 0:
                    exit(0)
            # loss计算
            iou_loss, obj_loss, total_loss = utils.loss.compute_loss(preds, targets, cfg, device)

            # 反向传播求解梯度
            total_loss.backward()

            #学习率预热
            for g in optimizer.param_groups:
                warmup_num =  5 * len(train_dataloader)
                if batch_num <= warmup_num:
                    scale = math.pow(batch_num/warmup_num, 4)
                    g["lr"] = cfg["opt"]["learning_rate"] * scale
                lr = g["lr"]

            # 更新模型参数
            if batch_num % cfg["opt"]["subdivisions"] == 0:
                optimizer.step()
                optimizer.zero_grad()

            # 打印相关信息
            info = "Epoch:%d LR:%f CIou:%f Obj:%f Total:%f" % (
                    epoch, lr, iou_loss, obj_loss, total_loss)
            pbar.set_description(info)

            batch_num += 1

        # 模型保存
        if epoch % 50 == 0 and epoch > 0:
            model.eval()
            #模型评估
            print("computer mAP...") # 使用很低的conf_thres看看模型是否能够能预测出来合适的box
            _, _, AP, _ = utils.utils.evaluation(val_dataloader, cfg, model, device)
            print("computer PR...") # 使用较高的conf_thres来看 p r f
            precision, recall, _, f1 = utils.utils.evaluation(val_dataloader, cfg, model, device, 0.3)
            print("Precision:%.2f%% Recall:%.2f%% AP:%.2f%% F1:%.2f%%"%(precision * 100, recall * 100, AP * 100, f1 * 100))
            os.makedirs("weights", exist_ok=True)
            torch.save(model.state_dict(), os.path.join(weights_dir, "%s-%d-epoch-%fap-model.pth" %
                      (cfg["model_name"], epoch, AP)))

        # 学习率调整
        scheduler.step()