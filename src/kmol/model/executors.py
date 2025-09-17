import math
from abc import ABCMeta
from copy import copy
from functools import partial
from pathlib import Path
from typing import List, Optional, Tuple
import gc
import json

import numpy as np
import torch
from torch.nn.modules.loss import _Loss as AbstractCriterion
from torch.optim import Optimizer as AbstractOptimizer
from torch.optim.lr_scheduler import _LRScheduler as AbstractLearningRateScheduler
from torch_geometric.data import Data
from torch_lr_finder import LRFinder
from torch_lr_finder.lr_finder import ExponentialLR

from kmol.model.architectures import AbstractNetwork, EnsembleNetwork
from kmol.model.metrics import PredictionProcessor
from kmol.model.trackers import ExponentialAverageMeter
from kmol.core.config import Config
from kmol.core.exceptions import CheckpointNotFound
from kmol.core.logger import LOGGER as logging
from kmol.core.helpers import Timer, SuperFactory, Namespace, HookProbe
from kmol.core.observers import EventManager
from kmol.core.custom_dataparallel import CustomDataParallel
from kmol.data.resources import Batch, LoadedContent
from kmol.core.utils import progress_bar


class AbstractExecutor(metaclass=ABCMeta):
    def __init__(self, config: Config):
        self.config = config
        self._timer = Timer()
        self._start_epoch = 0
        self._device = self.config.get_device()

        self.network = None
        self._setup_network()

        self.optimizer = None
        self.criterion = None
        self.scheduler = None

    def _to_device(self, batch):
        batch.outputs = batch.outputs.to(self._device)
        for key, values in batch.inputs.items():
            try:
                if isinstance(values, torch.Tensor) or issubclass(type(values), Data):
                    batch.inputs[key] = values.to(self._device)
                elif isinstance(values, dict):
                    batch.inputs[key] = self.dict_to_device(values)
                elif isinstance(values, list):
                    if isinstance(values[0], torch.Tensor):
                        batch.inputs[key] = [a.to(self._device) for a in values]
                    else:
                        batch.inputs[key] = [a for a in values]
                else:
                    batch.inputs[key] = values
            except (AttributeError, ValueError) as e:
                logging.debug(e)
                pass

    def dict_to_device(self, dict):
        new_dict = {}
        for k, v in dict.items():
            if type(v) is dict:
                new_dict[k] = self.dict_to_device(v)
            else:
                if isinstance(v, torch.Tensor) or issubclass(type(v), Data):
                    new_dict[k] = v.to(self._device)
                else:
                    new_dict[k] = v

        return new_dict

    def _load_checkpoint(self, train: bool = False) -> None:
        payload = Namespace(executor=self)
        EventManager.dispatch_event(event_name="before_checkpoint_load", payload=payload)
        self.network.load_checkpoint(self.config.checkpoint_path, self.config.get_device())

        if not self.config.is_finetuning and train:
            info = torch.load(self.config.checkpoint_path, map_location=self.config.get_device())

            if self.optimizer and "optimizer" in info:
                self.optimizer.load_state_dict(info["optimizer"])

            if self.scheduler and "scheduler" in info:
                self.scheduler.load_state_dict(info["scheduler"])

            if "epoch" in info:
                self._start_epoch = info["epoch"]

        payload = Namespace(executor=self)
        EventManager.dispatch_event(event_name="after_checkpoint_load", payload=payload)

    def _setup_network(self) -> None:
        self.network = SuperFactory.create(AbstractNetwork, self.config.model)

        payload = Namespace(executor=self, config=self.config)
        EventManager.dispatch_event(event_name="after_network_create", payload=payload)

        if self.config.should_parallelize():
            self.network = CustomDataParallel(self.network, device_ids=self.config.enabled_gpus)

        self.network.to(self.config.get_device())


class Trainer(AbstractExecutor):
    def __init__(self, config: Config):
        super().__init__(config)
        self._loss_tracker = ExponentialAverageMeter(smoothing_factor=0.95)

        self._metric_trackers = {name: ExponentialAverageMeter(smoothing_factor=0.9) for name in self.config.train_metrics}
        self._metric_computer = PredictionProcessor(
            metrics=self.config.train_metrics,
            threshold=self.config.threshold,
        )

    def _setup(self, training_examples: int) -> None:
        self.criterion = SuperFactory.create(AbstractCriterion, self.config.criterion).to(self.config.get_device())

        self.optimizer = SuperFactory.create(
            AbstractOptimizer,
            self.config.optimizer,
            {"params": self.network.parameters()},
        )

        self.scheduler = self._initialize_scheduler(optimizer=self.optimizer, training_examples=training_examples)

        try:
            self._load_checkpoint(train=True)
            self.anchor_params = {name: param.clone().detach() for name, param in self.network.named_parameters()}
            logging.info("Checkpoint loaded successfully")
        except CheckpointNotFound:
            pass

        self.network = self.network.train()
        logging.debug(self.network)

    def _initialize_scheduler(self, optimizer: AbstractOptimizer, training_examples: int) -> AbstractLearningRateScheduler:
        return SuperFactory.create(
            AbstractLearningRateScheduler,
            self.config.scheduler,
            {
                "optimizer": optimizer,
                "steps_per_epoch": math.ceil(training_examples / self.config.batch_size),
            },
        )

    def run(self, data_loader: LoadedContent, val_loader: Optional[LoadedContent] = None):
        self._setup(training_examples=data_loader.samples)

        initial_payload = Namespace(trainer=self, data_loader=data_loader)
        EventManager.dispatch_event(event_name="before_train_start", payload=initial_payload)
        best_metric = -np.inf
        for epoch in range(self._start_epoch + 1, self.config.epochs + 1):
            self._train_epoch(data_loader, epoch)
            val_metrics = self._validation(val_loader)
            best_metric, new_best = self._check_best(epoch, val_metrics, best_metric)

            self.log(epoch, val_metrics, new_best)
            self._reset_trackers()

        EventManager.dispatch_event(event_name="after_train_end", payload=initial_payload)

    def _training_step(self, batch, epoch):
        self._to_device(batch)
        self.optimizer.zero_grad()
        outputs = self.network(batch.inputs)

        payload = Namespace(features=batch, logits=outputs, extras=[], epoch=epoch, config=self.config)
        EventManager.dispatch_event(event_name="before_criterion", payload=payload)

        loss = self.criterion(payload.logits, payload.features.outputs, *payload.extras)

        loss.backward()

        self.optimizer.step()
        if self.config.is_stepwise_scheduler:
            self.scheduler.step()

        payload = Namespace(outputs=outputs)
        EventManager.dispatch_event(event_name="before_tracker_update", payload=payload)

        outputs = payload.outputs

        self._update_trackers(loss.item(), batch.outputs, outputs)

    def _train_epoch(self, train_loader, epoch):
        self.network.train()
        iteration = 1
        with progress_bar() as progress:
            description = f"Epoch {epoch} | Train Loss: {self._loss_tracker.get():.5f}"
            task = progress.add_task(description, total=len(train_loader.dataset))
            for batch in train_loader.dataset:
                self._training_step(batch, epoch)
                if iteration % self.config.log_frequency == 0:
                    description = f"Epoch {epoch} | Train Loss: {self._loss_tracker.get():.5f}"
                progress.update(task, description=description, advance=1)
                iteration += 1
            if not self.config.is_stepwise_scheduler:
                self.scheduler.step()

    @torch.no_grad()
    def _validation(self, val_loader):
        if val_loader is None:
            return Namespace()
        ground_truth = []
        logits = []
        self.network.eval()
        with progress_bar() as progress:
            for batch in progress.track(val_loader.dataset, description="Validating..."):
                self._to_device(batch)
                ground_truth.append(batch.outputs)

                payload = Namespace(logits=self.network(batch.inputs), logits_var=None, softmax_score=None)
                EventManager.dispatch_event(event_name="after_val_inference", payload=payload)

                logits.append(payload.logits)

            metrics = self._metric_computer.compute_metrics(ground_truth, logits)
            averages = self._metric_computer.compute_statistics(metrics, (np.mean,))

        return averages

    def _check_best(self, epoch, val_metrics, best_metric):
        if val_metrics == Namespace():
            new_best = True
            target_metric = best_metric
        else:
            target_metric = getattr(val_metrics, self.config.target_metric)[0]
            new_best = target_metric > best_metric
        if new_best:
            best_metric = target_metric
            self.save(epoch)
        return best_metric, new_best

    def _update_trackers(self, loss: float, ground_truth: torch.Tensor, logits: torch.Tensor) -> None:
        self._loss_tracker.update(loss)

        metrics = self._metric_computer.compute_metrics([ground_truth], [logits])
        averages = self._metric_computer.compute_statistics(metrics, (np.mean,))

        for metric_name, tracker in self._metric_trackers.items():
            tracker.update(getattr(averages, metric_name)[0])

    def _reset_trackers(self) -> None:
        self._loss_tracker.reset()

        for tracker in self._metric_trackers.values():
            tracker.reset()

    def save(self, epoch: int) -> None:
        info = {
            "epoch": epoch,
            "model": self.network.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "scheduler": self.scheduler.state_dict(),
        }
        suffix = "best" if self.config.overwrite_checkpoint else epoch
        model_path = Path(self.config.output_path) / f"checkpoint.{suffix}.pt"
        logging.info("Saving checkpoint: {}".format(model_path))

        payload = Namespace(info=info)
        EventManager.dispatch_event(event_name="before_checkpoint_save", payload=payload)

        torch.save(info, model_path)

    def log(self, epoch: int, val_metrics: Namespace, new_best: bool) -> None:
        message = "epoch: {} - Train loss: {:.4f} - time elapsed: {}".format(
            epoch,
            self._loss_tracker.get(),
            str(self._timer),
        )

        for name, tracker in self._metric_trackers.items():
            message += " - Train {}: {:.4f}".format(name, tracker.get())

        for name, value in vars(val_metrics).items():
            message += " - Val {}: {:.4f}".format(name, value[0])

        message += " (New best)" if new_best else ""

        payload = Namespace(
            message=message,
            epoch=epoch,
            trainer=self,
        )
        EventManager.dispatch_event(event_name="before_train_progress_log", payload=payload)

        logging.info(payload.message)

        with (Path(self.config.output_path) / "logs.txt").open("a") as f:
            f.write(message + "\n")


class Predictor(AbstractExecutor):
    def __init__(self, config: Config):
        super().__init__(config)

        self._load_checkpoint()
        self.network = self.network.eval()
        self.probe = None

        # Macros of observers are launched in criterions, so it should be initialized even in predictor
        criterion = SuperFactory.create(AbstractCriterion, self.config.criterion).to(self.config.get_device())
        print(self.network)

    def set_hook_probe(self):
        if isinstance(self.network, EnsembleNetwork):
            raise ValueError(
                "Probing hidden layers is not defined for Ensembles."
                " Please change 'probe_layer' parameter to 'null' or use a different type of network."
            )
        else:
            self.probe = HookProbe(self.network, self.config.probe_layer)

    def run(self, batch: Batch) -> torch.Tensor:
        self._to_device(batch)
        with torch.no_grad():
            if self.config.probe_layer is not None:
                self.set_hook_probe()

            payload = Namespace(data=batch.inputs, extras=[], loss_type=self.config.criterion["type"])
            EventManager.dispatch_event("before_predict", payload=payload)

            if self.config.inference_mode == "mc_dropout":
                outputs = self.network.mc_dropout(
                    batch.inputs,
                    dropout_prob=self.config.mc_dropout_probability,
                    n_iter=self.config.mc_dropout_iterations,
                    loss_type=self.config.criterion["type"],
                )
            else:
                outputs = self.network(payload.data, *payload.extras)

            if isinstance(outputs, torch.Tensor):
                outputs = {"logits": outputs}

            if self.probe is not None:
                outputs["hidden_layer"] = self.probe.get_probe()

            payload = Namespace(features=batch, **outputs)
            EventManager.dispatch_event("after_predict", payload=payload)

            return payload

    def run_all(self, data_loader: LoadedContent) -> Tuple[List[torch.Tensor], List[torch.Tensor]]:
        ground_truth = []
        logits = []

        with progress_bar() as progress:
            for batch in progress.track(data_loader.dataset, description="Evaluating..."):
                ground_truth.append(batch.outputs)
                logits.append(self.run(batch).logits)

        return ground_truth, logits


class Evaluator(AbstractExecutor):
    def __init__(self, config: Config):
        super().__init__(config)

        self._predictor = Predictor(config=self.config)
        self._processor = PredictionProcessor(metrics=self.config.test_metrics, threshold=self.config.threshold)

    def run(self, data_loader: LoadedContent) -> Namespace:
        ground_truth, logits = self._predictor.run_all(data_loader=data_loader)
        return self._processor.compute_metrics(ground_truth=ground_truth, logits=logits)


class Pipeliner(AbstractExecutor):
    def __init__(self, config: Config):
        self.config = config

        self._trainer = Trainer(self.config)
        self._processor = PredictionProcessor(metrics=self.config.test_metrics, threshold=self.config.threshold)
        self._predictor = None

    def initialize_predictor(self) -> "Pipeliner":
        self._predictor = Predictor(config=self.config)
        return self

    def train(self, data_loader: LoadedContent, val_loader: Optional[LoadedContent] = None) -> None:
        self._trainer.run(data_loader=data_loader, val_loader=val_loader)

    def evaluate(self, data_loader: LoadedContent) -> Namespace:
        ground_truth, logits = self.predict(data_loader=data_loader)
        return self._processor.compute_metrics(ground_truth=ground_truth, logits=logits)

    def predict(self, data_loader: LoadedContent) -> Tuple[List[torch.Tensor], List[torch.Tensor]]:
        return self._predictor.run_all(data_loader=data_loader)

    def evaluate_all(self, data_loader: LoadedContent) -> List[Namespace]:
        results = []

        for checkpoint_path in self.find_all_checkpoints():
            config = copy(self.config)
            config.checkpoint_path = checkpoint_path

            evaluator = Evaluator(config=config)
            results.append(evaluator.run(data_loader=data_loader))

        return results

    def find_all_checkpoints(self) -> List[str]:
        checkpoint_paths = Path(self.config.output_path).rglob("*.pt")
        checkpoint_paths = sorted([str(f) for f in checkpoint_paths], key=len)
        return checkpoint_paths

    def find_best_checkpoint(self, data_loader: LoadedContent) -> "Pipeliner":
        results = self.evaluate_all(data_loader=data_loader)

        per_target_best = Namespace.reduce(results, partial(np.argmax, axis=0))
        per_target_best = getattr(per_target_best, self.config.target_metric)
        all_checkpoints = self.find_all_checkpoints()

        self.config.checkpoint_path = all_checkpoints[np.argmax(np.bincount(per_target_best))]
        self.initialize_predictor()
        return self

    def get_network(self) -> AbstractNetwork:  # throws CheckpointNotFound Exception
        super().__init__(self.config)

        self._load_checkpoint()
        return self.network


class ThresholdFinder(Evaluator):
    def run(self, data_loader: LoadedContent) -> List[float]:
        ground_truth, logits = self._predictor.run_all(data_loader=data_loader)
        return self._processor.find_best_threshold(ground_truth=ground_truth, logits=logits)


class LearningRareFinder(Trainer, LRFinder):
    """
    Runs training for a given number of steps to find appropriate lr value.
    https://sgugger.github.io/how-do-you-find-a-good-learning-rate.html
    https://github.com/davidtvs/pytorch-lr-finder
    """

    END_LEARNING_RATE = 100
    START_LR = 1e-5
    NUM_ITERATION = 100
    DIVERGENCE_THRESHOLD = 5
    SMOOTHING_FACTOR = 0.05

    def _initialize_scheduler(self, optimizer: AbstractOptimizer, training_examples: int) -> AbstractLearningRateScheduler:
        return ExponentialLR(optimizer, self.END_LEARNING_RATE, self.NUM_ITERATION)

    def _set_learning_rate(self, new_lr):
        new_lrs = [new_lr] * len(self.optimizer.param_groups)
        for param_group, new_lr in zip(self.optimizer.param_groups, new_lrs):
            param_group["lr"] = new_lr

    def run(self, data_loader: LoadedContent) -> None:
        self.history = {"lr": [], "loss": []}
        self.best_loss = None

        self._setup(training_examples=data_loader.batches)
        self._set_learning_rate(self.START_LR)
        payload = Namespace(trainer=self, data_loader=data_loader)
        EventManager.dispatch_event(event_name="before_train_start", payload=payload)

        self._iterator = iter(data_loader.dataset)
        with progress_bar() as progress:
            task = progress.add_task("Loss: ", total=self.NUM_ITERATION)
            for iteration in range(self.NUM_ITERATION):
                try:
                    data = next(self._iterator)
                except StopIteration:
                    self._iterator = iter(data_loader.dataset)
                    data = next(self._iterator)
                self._to_device(data)
                self.optimizer.zero_grad()
                outputs = self.network(data.inputs)

                payload = Namespace(features=data, logits=outputs, extras=[])
                EventManager.dispatch_event(event_name="before_criterion", payload=payload)
                loss = self.criterion(payload.logits, payload.features.outputs, *payload.extras)
                loss.backward()

                self._loss_tracker.update(loss.item())
                smoothed_loss = self.track_best_loss(iteration, loss.item())
                self.history["lr"].append(self._get_learning_rate())
                self.optimizer.step()
                self.scheduler.step()
                self.history["loss"].append(smoothed_loss)
                description = f"Loss : {smoothed_loss:.5f} Lr: {self._get_learning_rate()}"
                progress.update(task, description=description)
                progress.advance(task, 1)
                del payload
                gc.collect()
                torch.cuda.empty_cache()
                if smoothed_loss > self.DIVERGENCE_THRESHOLD * self.best_loss:
                    break

        logging.info(
            "Learning rate search finished. The value below in an indication see the graph in the output directory for analysis"
        )
        self.plot(log_lr=True, skip_start=0, skip_end=2)
        plt.savefig(Path(self.config.output_path) / "lr_finder_results.png")
        with open(Path(self.config.output_path) / "history.json", "w") as file:
            json.dump(self.history, file)

    def track_best_loss(self, iteration, loss):
        if iteration == 0:
            self.best_loss = loss
            return loss

        if self.SMOOTHING_FACTOR > 0:
            smooth_loss = self.SMOOTHING_FACTOR * loss + (1 - self.SMOOTHING_FACTOR) * self.history["loss"][-1]
        if smooth_loss < self.best_loss:
            self.best_loss = smooth_loss

        return smooth_loss

    def _get_learning_rate(self) -> float:
        return self.optimizer.param_groups[0]["lr"]
