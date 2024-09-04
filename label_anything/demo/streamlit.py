import itertools
from einops import rearrange
import pandas as pd
import streamlit as st
import torch
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from streamlit_image_annotation import detection
from streamlit_drawable_canvas import st_canvas
from streamlit_tags import st_tags
from huggingface_hub import list_models

from label_anything import LabelAnything
from label_anything.data.dataset import LabelAnythingDataset, VariableBatchSampler
from label_anything.data.transforms import CustomResize, CustomNormalize
from accelerate import Accelerator

import numpy as np
import torch
from torchvision.transforms import Compose, PILToTensor
from torch.utils.data import DataLoader
import numpy as np
import os

import lovely_tensors as lt
from label_anything.demo.preprocess import preprocess_support_set, preprocess_to_batch
from label_anything.demo.utils import (
    COLORS,
    TEXT_COLORS,
    SupportExample,
    get_color_from_class,
    open_rgb_image,
    debug_write,
)
from label_anything.experiment.substitution import Substitutor
from label_anything.models.build_encoder import build_vit_b, build_vit_b_mae
from label_anything.utils.utils import ResultDict, load_yaml, torch_dict_load
from label_anything.models import model_registry

lt.monkey_patch()

from label_anything.models import build_lam_no_vit
from label_anything.data.examples import uniform_sampling
from label_anything.data import utils
from label_anything.data.utils import (
    AnnFileKeys,
    PromptType,
    BatchKeys,
    StrEnum,
    get_preprocess_shape,
)
from label_anything.experiment.utils import WrapperModule

from label_anything.demo.visualize import (
    draw_all,
    get_image,
    load_from_wandb,
    plot_seg,
)
from label_anything.demo.builtin import built_in_dataset, predict

import cv2
import matplotlib.pyplot as plt
from torchvision.transforms.functional import resize
from easydict import EasyDict


LONG_SIDE_LENGTH = 1024
IMG_DIR = "data/coco/train2017"
ANNOTATIONS_DIR = "data/annotations/instances_val2017.json"
EMBEDDINGS_DIR = "data/coco/embeddings"
MAX_EXAMPLES = 30
VIT_B_SAM_PATH = "checkpoints/sam_vit_b_01ec64.pth"

SIZE = 1024

preprocess = Compose([CustomResize(SIZE), PILToTensor(), CustomNormalize(SIZE)])


class SS(StrEnum):
    SUPPORT_SET = "support_set"
    CLASSES = "classes"


@st.cache_resource
def get_data(_accelerator):
    dataset = LabelAnythingDataset(
        {
            "coco": {
                "name": "coco",
                "instances_path": st.session_state.get(
                    "annotations_dir", ANNOTATIONS_DIR
                ),
                "img_dir": st.session_state.get("img_dir", IMG_DIR),
                "preprocess": preprocess,
            }
        },
        {},
    )
    sampler = VariableBatchSampler(
        dataset,
        possible_batch_example_nums=[[1, 8], [1, 4], [1, 2], [1, 1]],
        num_processes=1,
        shuffle=False,
    )
    dataloader = DataLoader(
        dataset=dataset, batch_sampler=sampler, collate_fn=dataset.collate_fn
    )
    dataloader = _accelerator.prepare(dataloader)
    return dataloader


@st.cache_resource
def load_model(_accelerator: Accelerator, checkpoint, model_load_mode):
    if model_load_mode == "Hugging Face":
        model = LabelAnything.from_pretrained(checkpoint)
        model = _accelerator.prepare(model)
        return model.model
    elif model_load_mode == "Wandb":
        folder = "best"
        model_file, config_file = load_from_wandb(checkpoint, folder)
        if config_file is not None:
            config = load_yaml(config_file)
            model_params = config["model"]
            name = model_params.pop("name")
        else:
            model_params = {}
            name = "lam_no_vit"
            st.warning(
                f"Config file not found, using default model params: {model_params}, {name}"
            )
        model = model_registry[name](**model_params)
        model = WrapperModule(model, None)
        model_state_dict = torch_dict_load(model_file)
        unmatched_keys = model.load_state_dict(model_state_dict, strict=False)
        model = _accelerator.prepare(model)
        if unmatched_keys.missing_keys:
            st.warning(f"Missing keys: {unmatched_keys.missing_keys}")
        if unmatched_keys.unexpected_keys and unmatched_keys.unexpected_keys != [
                    "loss.prompt_components.prompt_contrastive.t_prime",
                    "loss.prompt_components.prompt_contrastive.bias",
                ]:
            st.warning(f"Unexpected keys: {unmatched_keys.unexpected_keys}")
        return model.model
    else:
        st.warning("Model load mode not supported")
        return None


def reset_support(idx):
    if idx is None:
        st.session_state[SS.SUPPORT_SET] = []
        st.session_state[SS.CLASSES] = []
        return
    st.session_state[SS.SUPPORT_SET].pop(idx)


def build_support_set():
    if st.session_state.get(SS.SUPPORT_SET, None) is None:
        st.session_state[SS.SUPPORT_SET] = []
    st.write("Choose the classes you want to segment in the image")
    cols = st.columns(2)
    with cols[0]:
        # classes = st_tags(
        #     label=SS.CLASSES,
        #     text="Type and press enter",
        #     value=st.session_state.get(SS.CLASSES, []),
        #     suggestions=["person", "car", "dog", "cat", "bus", "truck"],
        # )
        new_class = st.text_input("Type and press enter to add a class")
        classes = st.session_state.get(SS.CLASSES, [])
        if new_class not in classes and new_class != "":
            classes.append(new_class)
        st.session_state[SS.CLASSES] = classes
    with cols[1]:
        if st.button("Reset"):
            reset_support(None)
            classes = []
    if not classes:
        return
    st.write("Classes:", ", ".join(classes))
    st.write("## Upload and annotate the support images")

    support_image = st.file_uploader(
        "If you want, you can upload and annotate another support image",
        type=["png", "jpg"],
        key="support_image",
    )
    if support_image is not None:
        add_support_image(support_image)


def add_support_image(support_image):
    support_image = open_rgb_image(support_image)
    st.write(
        "Use the annotation tool to annotate the image with bounding boxes, click Complete when you are done"
    )
    tab1, tab2 = st.tabs(["Annotate", "Load mask"])
    with tab1:
        cols = st.columns(3)
        with cols[0]:
            selected_class = st.selectbox(
                "Select the class you want to annotate",
                st.session_state[SS.CLASSES],
                key="selectbox_class",
            )
        with cols[1]:
            prompt_type = st.selectbox(
                "Prompt Type", ["rect", "point", "polygon"], key="drawing_mode"
            )
        with cols[2]:
            edit_mode = st.checkbox("Edit annotations", key="edit_mode")
        edit_mode = "transform" if edit_mode else prompt_type
        selected_class_color_f, selected_class_color_st = get_color_from_class(
            st.session_state[SS.CLASSES], selected_class
        )
        shape = get_preprocess_shape(
            support_image.size[1], support_image.size[0], LONG_SIDE_LENGTH
        )
        results = st_canvas(
            fill_color=selected_class_color_f,  # Fixed fill color with some opacity
            stroke_color=selected_class_color_st,  # Fixed stroke color with full opacity
            background_image=support_image,
            drawing_mode=edit_mode,
            key="input_prompt_detection",
            width=shape[1],
            height=shape[0],
            stroke_width=2,
            update_streamlit=False,
        )
    with tab2:
        st.write("Load a mask to segment the image")
        st.write("Select the color for each class (background is always black)")
        color_map = {}
        color_cols = st.columns(len(st.session_state[SS.CLASSES]))
        for i, cls in enumerate(st.session_state[SS.CLASSES]):
            with color_cols[i]:
                color = st.selectbox(
                    f"Select color for {cls}",
                    TEXT_COLORS,
                    key=f"color_{cls}",
                    index=i,
                )
                color_map[i] = np.array(COLORS[TEXT_COLORS.index(color)])
        mask = st.file_uploader(
            "Upload the mask",
            type=["png", "jpg"],
            accept_multiple_files=False,
            key="mask_support",
        )
        mask = np.array(open_rgb_image(mask)) if mask is not None else None
        st.image(mask, caption="Mask", use_column_width=True) if mask is not None else None
        if mask is not None:
            results = {
                "mask": mask,
                "color_map": color_map,
            }
    if results is not None and st.button("Add Support Image"):
        example = SupportExample(support_image=support_image)
        if hasattr(results, "json_data") and results.json_data is not None:
            st.write("Extracting prompts from canvas")
            example.prompts = results.json_data
            example.reshape = shape
        if isinstance(results, dict) and "mask" in results:
            st.write("Extracting prompts from mask")
            example.prompts = results
            example.reshape = shape
        st.session_state[SS.SUPPORT_SET].append(example)
        st.session_state.pop("input_prompt_detection", None)
        st.session_state.pop("mask_support", None)
        st.session_state.pop("support_image", None)
        st.write("Support image added")
        
        
def preview_support_set(batch, preview_cols):
    for i, elem in enumerate(st.session_state[SS.SUPPORT_SET]):
        img = batch[BatchKeys.IMAGES][0][i]
        masks = batch[BatchKeys.PROMPT_MASKS][0][i]
        bboxes = batch[BatchKeys.PROMPT_BBOXES][0][i]
        points = batch[BatchKeys.PROMPT_POINTS][0][i]
        img = get_image(img)
        img = draw_all(img, masks=masks, boxes=bboxes, points=points, colors=COLORS)
        with preview_cols[i]:
            if st.button(f"Remove Image {i+1}"):
                reset_support(i)
            st.image(img, caption=f"Support Image {i+1}", use_column_width=True)
            

def try_it_yourself(model, image_encoder):
    st.write("Upload the image the you want to segment")
    query_images = st.file_uploader(
        "Choose an image", type=["png", "jpg"], accept_multiple_files=True
    )
    if len(query_images) > 0:
        images = [open_rgb_image(query_image) for query_image in query_images]
        with st.expander("Query Images"):
            cols = st.columns(len(query_images))
            for i, query_image in enumerate(query_images):
                image = open_rgb_image(query_image)
                with cols[i]:
                    # Save image in a temp file
                    st.image(image, caption=f"Query Image {i+1}", width=300)
    build_support_set()
    if SS.SUPPORT_SET in st.session_state and len(st.session_state[SS.SUPPORT_SET]) > 0:
        preview_cols = st.columns((len(st.session_state[SS.SUPPORT_SET])))
        batch = preprocess_support_set(
                st.session_state[SS.SUPPORT_SET],
                list(range(len(st.session_state[SS.CLASSES]))),
                size=st.session_state.get("image_size", 1024),
            )
        preview_support_set(batch, preview_cols)

    if (
        SS.SUPPORT_SET in st.session_state and len(st.session_state[SS.SUPPORT_SET]) > 0
        and SS.CLASSES in st.session_state
        and len(query_images) > 0
    ):
        batches = [
            preprocess_to_batch(
                image,
                batch.copy(),
                size=st.session_state.get("image_size", 1024),
            )
            for image in images
        ]
        st.write(batches)
        if st.button("Predict"):
            progress = st.progress(0)
            tabs = st.tabs([f"Query Image {i+1}" for i in range(len(batches))])
            for i, batch in enumerate(batches):
                with tabs[i]:
                    result = predict(model, image_encoder, batch)
                    pred = result[ResultDict.LOGITS].argmax(dim=1)
                    st.json(result, expanded=False)
                    plots, titles = plot_seg(
                        batch,
                        pred,
                        COLORS,
                        dims=batch[BatchKeys.DIMS],
                        classes=st.session_state[SS.CLASSES],
                    )
                    cols = st.columns(2)
                    cols[0].image(plots[0], caption=titles[0], use_column_width=True)
                    cols[1].image(plots[1], caption=titles[1], use_column_width=True)
                    progress.progress((i + 1) / len(batches))


def main():
    st.set_page_config(layout="wide", page_title="Label Anything")
    st.title("Label Anything")
    st.sidebar.title("Settings")
    accelerator = Accelerator()
    with st.sidebar:
        # load model
        model_load_mode = st.radio("Load model", ["Hugging Face", "Wandb"], index=0)
        if model_load_mode == "Hugging Face":
            models = [model.id for model in list_models(author="pasqualedem") if model.id.startswith("pasqualedem/label_anything")]
            checkpoint = st.selectbox("Model", models)
        elif model_load_mode == "Wandb":
            checkpoint = st.text_input("Wandb run id", "3ndl7had")
        model = load_model(accelerator, checkpoint, model_load_mode)
        st.session_state["image_size"] = 1024 #TODO remove this
        image_encoder = model.image_encoder
        st.divider()
        st.json(st.session_state, expanded=False)
    tiy_tab, dataset_tab = st.tabs(["Try it yourself", "Built-in dataset"])
    with tiy_tab:
        try_it_yourself(model, image_encoder)
    with dataset_tab:
        built_in_dataset(accelerator, model)


if __name__ == "__main__":
    main()
