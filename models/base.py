import os
import sys

import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
from pl_bolts.optimizers.lars_scheduling import LARSWrapper
from pl_bolts.optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from torch.optim.lr_scheduler import (
    CosineAnnealingLR,
    ExponentialLR,
    MultiStepLR,
    ReduceLROnPlateau,
)

try:
    from resnet import resnet18, resnet50
except:
    from .resnet import resnet18, resnet50

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))

from utils.metrics import accuracy_at_k, weighted_mean


class BaseModel(pl.LightningModule):
    def __init__(self, args):
        super().__init__()
        self.args = args

    def configure_optimizers(self):
        # select optimizer
        if self.args.optimizer == "sgd":
            optimizer = torch.optim.SGD
        else:
            optimizer = torch.optim.Adam

        if hasattr(self, "student"):
            model_with_parameters = self.student
        else:
            model_with_parameters = self

        if hasattr(model_with_parameters, "classifier"):
            classifier_parameters = model_with_parameters.classifier.parameters()
            other_parameters = (
                p
                for name, p in model_with_parameters.named_parameters()
                if "classifier" not in name
            )
            parameters = [
                {"params": other_parameters},
                {"params": classifier_parameters, "lr": 0.3, "weight_decay": 0},
            ]
        else:
            parameters = model_with_parameters.parameters()

        optimizer = optimizer(
            parameters,
            lr=self.args.lr,
            weight_decay=self.args.weight_decay,
            **self.args.extra_optimizer_args,
        )
        if self.args.lars:
            optimizer = LARSWrapper(optimizer)

        if self.args.scheduler == "none":
            return optimizer
        else:
            if self.args.scheduler == "warmup_cosine":
                scheduler = LinearWarmupCosineAnnealingLR(
                    optimizer,
                    warmup_epochs=10,
                    max_epochs=self.args.epochs,
                    warmup_start_lr=0.003,
                )
            if self.args.scheduler == "cosine":
                scheduler = CosineAnnealingLR(optimizer, self.args.epochs)
            elif self.args.scheduler == "reduce":
                scheduler = ReduceLROnPlateau(optimizer)
            elif self.args.scheduler == "step":
                scheduler = MultiStepLR(optimizer, self.args.lr_decay_steps)
            elif self.args.scheduler == "exponential":
                scheduler = ExponentialLR(optimizer, self.args.weight_decay)
            return [optimizer], [scheduler]

    def validation_step(self, batch, batch_idx):
        X, target = batch
        batch_size = X.size(0)

        output = self(X)
        loss = F.cross_entropy(output, target)

        acc1, acc5 = accuracy_at_k(output, target, top_k=(1, 5))

        results = {
            "batch_size": batch_size,
            "val_loss": loss,
            "val_acc1": acc1,
            "val_acc5": acc5,
        }
        return results

    def validation_epoch_end(self, outputs):
        val_loss = weighted_mean(outputs, "val_loss", "batch_size")
        val_acc1 = weighted_mean(outputs, "val_acc1", "batch_size")
        val_acc5 = weighted_mean(outputs, "val_acc5", "batch_size")

        log = {"val_loss": val_loss, "val_acc1": val_acc1, "val_acc5": val_acc5}
        self.log_dict(log, prog_bar=True, sync_dist=True)


class Model(BaseModel):
    def __init__(self, args):
        super().__init__(args)

        assert args.encoder in ["resnet18", "resnet50"]

        if args.encoder == "resnet18":
            base_model = resnet18
            self.features_size = 512
        else:
            self.features_size = 2048
            base_model = resnet50

        # initialize encoder
        self.encoder = base_model(
            cifar=args.cifar,
            projection_bn=args.projection_bn,
            hidden_mlp=args.hidden_mlp,
            output_dim=args.encoding_size,
        )
        self.classifier = nn.Linear(self.features_size, args.n_classes)

        self.encoding_size = args.encoding_size
        self.temperature = args.temperature

    def forward(self, X, classify_only=True):
        features, z = self.encoder(X)
        # stop gradients from the classifier
        y = self.classifier(features.detach())

        if classify_only:
            return y

        return features, z, y