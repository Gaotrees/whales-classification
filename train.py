import os
import sys
import argparse
from datetime import datetime
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from tqdm import tqdm

import torch
from torch.optim import Adam, SGD
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms
from torch.optim.lr_scheduler import StepLR, MultiStepLR

from sklearn.metrics.pairwise import cosine_similarity

from backbones.Resnet34 import FaceNetModel
from backbones.InceptionResnet import InceptionResnetV1
from dataloader import WhalesData, data_transform, data_transform_test
from sampler import PKSampler
from utils import get_lr, log_experience
from losses import TripletLoss

parser = argparse.ArgumentParser()
parser.add_argument(
    '--root', default='/data_science/computer_vision/whales/data/train/', type=str)
parser.add_argument(
    '--root-test', default='/data_science/computer_vision/whales/data/test_val/', type=str)

parser.add_argument('--archi', default='resnet34',
                    choices=['resnet34', 'inception'], type=str)
parser.add_argument('--embedding-dim', type=int, default=256)
parser.add_argument('--pretrained', type=int, choices=[0, 1], default=1)
parser.add_argument('--margin', type=float, default=0.2)

parser.add_argument('-p', type=int, default=8)
parser.add_argument('-k', type=int, default=4)


parser.add_argument('--lr', type=float, default=3e-4)
parser.add_argument('--epochs', type=int, default=80)
parser.add_argument('--batch-size', type=int, default=32)
parser.add_argument('--num-workers', type=int, default=11)
parser.add_argument('--gamma', type=float, default=0.1)

parser.add_argument('--logging-step', type=int, default=10)
parser.add_argument('--output', type=str, default='./models/')
parser.add_argument('--submissions', type=str, default='./submissions/')
parser.add_argument('--logs-experiences', type=str,
                    default='./experiences/logs.csv')

parser.add_argument('--bbox-train', type=str,
                    default='/data_science/computer_vision/whales/bounding_boxes/train_bbox.csv')
parser.add_argument('--bbox-test', type=str,
                    default='/data_science/computer_vision/whales/bounding_boxes/test_bbox.csv')
parser.add_argument('--bbox-all', type=str,
                    default='/data_science/computer_vision/whales/bounding_boxes/all_bbox.csv')

parser.add_argument('--checkpoint', type=str, default=None)

np.random.seed(0)
torch.manual_seed(0)

args = parser.parse_args()
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


def main():
    time_id = log_experience(args)

    train_path = args.root
    classes = os.listdir(train_path)
    classes.remove('-1')
    num_classes = len(classes)
    mapping_label_id = dict(zip(classes, range(len(classes))))

    mapping_files_to_global_id = {}
    mapping_global_id_to_files = {}

    paths = []
    index = 0
    labels_to_samples = {}
    for c in classes:
        labels_to_samples[c] = os.listdir(os.path.join(train_path, c))
        for f in os.listdir(os.path.join(train_path, c)):
            file_path = str(os.path.join(train_path, c, f))
            mapping_files_to_global_id[file_path] = index
            mapping_global_id_to_files[index] = file_path
            index += 1
            paths.append(file_path)

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    if args.checkpoint is not None:
        model = FaceNetModel(args.embedding_dim,
                             num_classes=num_classes,
                             pretrained=bool(args.pretrained))
        weights = torch.load(args.checkpoint)
        model.load_state_dict(weights.state_dict())
        print('loading saved model ...')
    else:
        if args.archi == 'resnet34':
            model = FaceNetModel(args.embedding_dim,
                                 num_classes=num_classes,
                                 pretrained=bool(args.pretrained))
        elif args.archi == 'inception':
            model = InceptionResnetV1(num_classes=num_classes,
                                      embedding_dim=args.embedding_dim)

    model.to(device)

    dataset = WhalesData(paths=paths,
                         bbox=args.bbox_train,
                         mapping_label_id=mapping_label_id,
                         transform=data_transform
                         )
    sampler = PKSampler(root=args.root,
                        data_source=dataset,
                        classes=classes,
                        labels_to_samples=labels_to_samples,
                        mapping_files_to_global_id=mapping_files_to_global_id,
                        p=args.p,
                        k=args.k)

    dataloader = DataLoader(dataset,
                            batch_size=args.p*args.k,
                            sampler=sampler,
                            num_workers=args.num_workers)

    criterion = TripletLoss(margin=args.margin, sample=False)
    optimizer = Adam(model.parameters(), lr=args.lr)
    scheduler = MultiStepLR(optimizer, milestones=[50], gamma=args.gamma)

    model.train()

    for epoch in tqdm(range(args.epochs)):
        current_lr = get_lr(optimizer)
        params = {
            'model': model,
            'dataloader': dataloader,
            'optimizer': optimizer,
            'criterion': criterion,
            'scheduler': scheduler,
            'logging_step': args.logging_step,
            'epoch': epoch,
            'epochs': args.epochs,
            'current_lr': current_lr,
        }
        _ = train(**params)
        scheduler.step()

    torch.save(model.state_dict(),
               os.path.join(args.output,
                            f'{time_id}_pth'))

    compute_predictions(model, mapping_label_id, time_id)


def train(model, dataloader, optimizer, criterion, scheduler, logging_step, epoch, epochs, current_lr):
    current_lr = get_lr(optimizer)
    losses = []

    for i, batch in tqdm(enumerate(dataloader), total=len(dataloader), leave=False):
        images = batch['image']
        targets = batch['label']
        predictions = model(images.cuda())
        loss = criterion(predictions, targets.cuda())
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        losses.append(loss.item())

        if i % logging_step == 0:
            running_avg_loss = np.mean(losses)
            print(
                f'[Epoch {epoch+1}][Batch {i} / {len(dataloader)}][lr: {current_lr}]: loss {running_avg_loss}')

    scheduler.step()
    average_loss = np.mean(losses)
    return average_loss


def compute_predictions(model, mapping_label_id, time_id):
    model.eval()
    print("generating predictions ...")
    db = []
    train_folder = os.path.join(args.root)
    for c in os.listdir(train_folder):
        for f in os.listdir(os.path.join(train_folder, c)):
            db.append(os.path.join(train_folder, c, f))

    db += [os.path.join(args.root_test, f) for f in os.listdir(args.root_test)]
    test_db = sorted(
        [os.path.join(args.root_test, f) for f in os.listdir(args.root_test)])

    scoring_dataset = WhalesData(db,
                                 args.bbox_all,
                                 mapping_label_id,
                                 data_transform)

    scoring_dataloader = DataLoader(scoring_dataset,
                                    shuffle=False,
                                    num_workers=10,
                                    batch_size=32)

    embeddings = []
    for batch in tqdm(scoring_dataloader, total=len(scoring_dataloader)):
        with torch.no_grad():
            embedding = model(batch['image'].cuda())
            embedding = embedding.cpu().detach().numpy()
            embeddings.append(embedding)
    embeddings = np.concatenate(embeddings)

    test_dataset = WhalesData(test_db,
                              args.bbox_test,
                              mapping_label_id,
                              data_transform_test)
    test_dataloader = DataLoader(test_dataset,
                                 shuffle=False,
                                 batch_size=32)

    test_embeddings = []
    for batch in tqdm(test_dataloader, total=len(test_dataloader)):
        with torch.no_grad():
            embedding = model(batch['image'].cuda())
            embedding = embedding.cpu().detach().numpy()
            test_embeddings.append(embedding)

    test_embeddings = np.concatenate(test_embeddings)

    csm = cosine_similarity(test_embeddings, embeddings)
    all_indices = []
    for i in range(len(csm)):
        test_file = test_db[i]
        index_test_file_all = db.index(test_file)
        similarities = csm[i]

        index_sorted_sim = np.argsort(similarities)[::-1]

        c = 0
        indices = []
        for idx in index_sorted_sim:
            if idx != index_test_file_all:
                indices.append(idx)
                c += 1
            if c > 20:
                break
        all_indices.append(indices)

    submission = pd.DataFrame(all_indices)
    submission = submission.rename(columns=dict(
        zip(submission.columns.tolist(), [c+1 for c in submission.columns.tolist()])))

    for c in submission.columns:
        submission[c] = submission[c].map(lambda v: db[v].split('/')[-1])

    submission[0] = [f.split('/')[-1] for f in test_db]
    submission = submission[range(21)]

    submission.to_csv(os.path.join(
        args.submissions, f'{time_id}.csv'), header=None, sep=',', index=False)
    print("predictions generated...")


if __name__ == "__main__":
    main()
