import gc
import os
import contextlib
from copy import deepcopy

import comet_ml
import torch
from accelerate import Accelerator
from accelerate.logging import get_logger

from label_anything.data.utils import random_batch
from label_anything.logger.image_logger import Logger
from label_anything.logger.text_logger import get_logger
from label_anything.utils.utils import find_divisor_pairs, get_divisors

logger = get_logger(__name__)


def parse_params(params_dict):
    train_params = params_dict.get("train_params", {})
    dataset_params = params_dict.get("dataset", {})
    model_params = params_dict.get("model", {})
    dataloader_params = params_dict.get("dataloader", {})

    return train_params, dataset_params, dataloader_params, model_params


def comet_experiment(comet_information: dict, accelerator: Accelerator, params: dict):
    global logger
    logger_params = deepcopy(params.get("logger", {}))
    logger_params.pop("comet", None)
    if (
        os.environ.get("TMPDIR", None)
        or os.environ.get("TMP", None)
        or os.environ.get("TEMP", None)
    ):
        if os.environ.get("TMPDIR", None):
            tmp_dir = os.environ.get("TMPDIR")
        elif os.environ.get("TMP", None):
            tmp_dir = os.environ.get("TMP")
        else:
            tmp_dir = os.environ.get("TEMP")
        logger.info(
            f"Using {tmp_dir} as temporary directory from environment variables"
        )
        logger_params["tmp_dir"] = tmp_dir
    else:
        tmp_dir = logger_params.get("tmp_dir", None)
        logger.info(
            f"No temporary directory found in environment variables, using {tmp_dir} for images"
        )
    os.makedirs(tmp_dir, exist_ok=True)
    tags = comet_information.pop("tags", [])

    if comet_information.pop("offline"):
        offdir = comet_information.pop("offline_directory", None)
        experiment = comet_ml.OfflineExperiment(
            offline_directory=offdir, **comet_information
        )
    else:
        experiment = comet_ml.Experiment(**comet_information)
    comet_ml.init(comet_information)
    experiment.add_tags(tags)
    experiment.log_parameters(params)

    return Logger(experiment, accelerator, **logger_params)


def get_batch_size(batch_tuple):
    if batch_tuple[0].get("images") is not None:
        return batch_tuple[0]["images"].shape[0]
    if batch_tuple[0].get("embeddings") is not None:
        return batch_tuple[0]["embeddings"].shape[0]


def get_example_class_size(batch_input):
    if batch_input.get("prompt_points") is not None:
        return (
            batch_input["prompt_points"].shape[1],
            batch_input["prompt_points"].shape[2],
        )
    if batch_input.get("prompt_bboxes") is not None:
        return (
            batch_input["prompt_bboxes"].shape[1],
            batch_input["prompt_bboxes"].shape[2],
        )
    if batch_input.get("prompt_masks") is not None:
        return (
            batch_input["prompt_masks"].shape[1],
            batch_input["prompt_masks"].shape[2],
        )


def check_nan(model, input_dict, output, gt, loss, step, train_params):
    if not train_params.get("check_nan", False):
        return
    if step % train_params["check_nan"] != 0:
        return
    if torch.isnan(loss) or loss.detach() in [torch.inf, -torch.inf]:
        if (
            train_params["check_nan"] == 1
        ):  # Makes sense only if we are checking every step
            state_dict = {
                "model": model.state_dict(),
                "input_dict": input_dict,
                "loss": loss,
                "step": step,
                "gt": gt,
                "output": output,
            }
            torch.save(state_dict, "nan.pt")
        raise ValueError("NaNs in loss")


def handle_oom(model, input_dict, batch_tuple, optimizer, gt, epoch, step):
    logger.warning(f"OOM at step {step}")
    logger.warning(torch.cuda.memory_summary())
    torch.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "input_dict": input_dict,
            "batch_tuple": batch_tuple,
            "gt": gt,
        },
        f"oom_epoch_{epoch}_step_{step}.pt",
    )
    optimizer.zero_grad()
    del input_dict
    del batch_tuple
    del gt
    gc.collect()
    torch.cuda.empty_cache()


def allocate_memory(model, accelerator, optimizer, criterion, dataloader):
    """
    Execute forward and backward with maximum input lenght to avoid out of memories
    """
    depth = 256
    height = 1024
    width = 1024
    num_classes = 10
    num_objects = 10

    max_num_images = dataloader.batch_sampler.get_max_num_images()
    batch_size_examples_pairs = find_divisor_pairs(max_num_images)
    logger.info(f"Max number of images: {max_num_images}")
    for batch_size, num_examples in batch_size_examples_pairs:
        optimizer.zero_grad()
        batch_dict, gt = random_batch(
            batch_size, num_examples, depth, height, width, num_classes, num_objects
        )
        batch_dict = accelerator.prepare(batch_dict)  # TODO: Make this work
        outputs = model(batch_dict)
        loss = criterion(outputs, gt)
        pred = outputs.argmax(dim=1)
        accelerator.backward(loss)
        optimizer.step()
        logger.info(f"Batch size {batch_size}; num examples: {num_examples} OK")
    logger.info("Allocating memory test PASSED")
    logger.info(torch.cuda.mem_get_info())


def set_class_embeddings(
    model,
    examples,
):
    examples = {k: v.unsqueeze(dim=0).to(model.device) for k, v in examples.items()}
    example_size, num_classes = get_example_class_size(examples)
    chunk_sizes = [None] + list(reversed(get_divisors(example_size * num_classes)))
    chunk_sizes = [1]
    passed = False
    i = 0
    while not passed and i < len(chunk_sizes):
        try:
            with torch.no_grad():
                class_embeddings = model.generate_class_embeddings(
                    examples, chunk_size=chunk_sizes[i]
                )
            passed = True
        except RuntimeError as e:
            if "out of memory" in str(e):
                gc.collect()
                torch.cuda.empty_cache()
                logger.warning(
                    f"Out of memory while generating class embeddings with chunk size {chunk_sizes[i]}"
                )
                exc = e
            else:
                raise e
        i += 1
    if not passed:
        logger.error(
            f"Out of memory while generating class embeddings, raising exception"
        )
        raise exc
    if isinstance(model, torch.nn.parallel.DistributedDataParallel):
        model.module.class_embeddings = class_embeddings
    else:
        model.class_embeddings = class_embeddings
    return model


@contextlib.contextmanager
def nosync_accumulation(accumulate=False, accelerator=None, model=None):
    if accumulate:
        with accelerator.no_sync(model):
            yield
    else:
        with contextlib.nullcontext():
            yield