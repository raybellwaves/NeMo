# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys

import pytest
import pytorch_lightning as pl
import torch
from omegaconf import OmegaConf

from nemo.core.classes import ModelPT
from nemo.utils.exp_manager import exp_manager

try:
    # `ptl_resiliency` is included in `gwe_resiliency_pkg` package
    from ptl_resiliency import StragglerDetectionCallback

    HAVE_STRAGGLER_DET = True
except (ImportError, ModuleNotFoundError):
    HAVE_STRAGGLER_DET = False


class OnesDataset(torch.utils.data.Dataset):
    def __init__(self, dataset_len):
        super().__init__()
        self.__dataset_len = dataset_len

    def __getitem__(self, *args):
        return torch.ones(2)

    def __len__(self):
        return self.__dataset_len


class StreamWrapper:
    # stream wrapper that writes to both a stream and a log file
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = open(log_file, 'w')

    def write(self, message):
        self.stream.write(message)
        self.log_file.write(message)

    def flush(self):
        self.stream.flush()
        self.log_file.flush()

    def close(self):
        self.stream.close()
        self.log_file.close()


class ExampleModel(ModelPT):
    def __init__(self, log_dir, **kwargs):
        cfg = OmegaConf.structured({})
        super().__init__(cfg)
        pl.seed_everything(1234)
        self.l1 = torch.nn.modules.Linear(in_features=2, out_features=1)
        self.log_dir = log_dir
        self.stdout_wrapper = None
        self.stderr_wrapper = None

    def on_train_start(self):
        super().on_train_start()
        rank = torch.distributed.get_rank()
        if not isinstance(sys.stdout, StreamWrapper):
            sys.stdout = StreamWrapper(sys.stdout, self.log_dir / f"stdout{rank}.log")
        if not isinstance(sys.stderr, StreamWrapper):
            sys.stderr = StreamWrapper(sys.stderr, self.log_dir / f"stderr{rank}.log")

    def train_dataloader(self):
        dataset = OnesDataset(128)
        return torch.utils.data.DataLoader(dataset, batch_size=2, num_workers=8)

    def val_dataloader(self):
        dataset = OnesDataset(128)
        return torch.utils.data.DataLoader(dataset, batch_size=2, num_workers=8)

    def forward(self, batch):
        output = self.l1(batch)
        output = torch.nn.functional.l1_loss(output, torch.zeros(output.size()).to(output.device))
        return output

    def validation_step(self, batch, batch_idx):
        self.loss = self(batch)
        return self.loss

    def training_step(self, batch, batch_idx):
        return self(batch)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=0.1)

    def list_available_models(self, *args, **kwargs):
        pass

    def setup_training_data(self, *args, **kwargs):
        pass

    def setup_validation_data(self, *args, **kwargs):
        pass

    def on_validation_epoch_end(self):
        self.log("val_loss", torch.stack([self.loss]).mean())


@pytest.mark.skipif(not HAVE_STRAGGLER_DET, reason="requires resiliency package to be installed.")
class TestStragglerDetection:

    @pytest.mark.run_only_on('GPU')
    def test_prints_perf_scores(self, tmp_path):
        # Run dummy, 1 rank DDP training, with worker stdout and stderr redirected to a file
        # Training time is limited to 3 seconds and straggler reporting is set to 1 second
        # Check if there are straggler related logs in the captured rank0 stdout
        max_steps = 1_000_000
        tmp_path = tmp_path / "test_1"

        trainer = pl.Trainer(
            strategy='ddp',
            devices=1,
            accelerator='gpu',
            enable_checkpointing=False,
            logger=False,
            max_steps=max_steps,
            val_check_interval=0.33,
        )
        exp_manager(
            trainer,
            {
                "max_time_per_run": "00:00:00:03",
                "explicit_log_dir": str(tmp_path),
                "create_checkpoint_callback": False,
                "create_straggler_detection_callback": True,
                "straggler_detection_params": {
                    "report_time_interval": 1.0,
                    "calc_relative_gpu_perf": True,
                    "calc_individual_gpu_perf": True,
                    "print_gpu_perf_scores": True,
                },
            },
        )
        model = ExampleModel(log_dir=tmp_path)
        trainer.fit(model)

        rank0_stdout_content = None
        with open(tmp_path / "stdout0.log") as f:
            rank0_stdout_content = f.read()

        assert "GPU relative performance" in rank0_stdout_content
        assert "GPU individual performance" in rank0_stdout_content
