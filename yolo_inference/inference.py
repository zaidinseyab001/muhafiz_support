"""YOLO detection + tracking wrapper.

One YoloInference instance per WebSocket connection. Ultralytics keeps
tracker state on the model's predictor object, so giving each camera
stream its own model instance is the simplest way to keep their track
ids isolated.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import torch
from ultralytics import YOLO

import config

log = logging.getLogger(__name__)


class YoloInference:
    def __init__(
        self,
        model_path: str = config.MODEL_PATH,
        device: str = config.DEVICE,
        use_half: bool = config.USE_HALF,
        tracker_cfg: str = config.TRACKER_CONFIG,
    ) -> None:
        log.info(
            "Loading YOLO model=%s device=%s half=%s tracker=%s",
            model_path, device, use_half, tracker_cfg,
        )
        self.model = YOLO(model_path)
        self.model.to(device)
        self.device = device
        self.use_half = use_half
        self.tracker_cfg = tracker_cfg

    def track(self, frame: np.ndarray, classes: Optional[list[int]] = config.CLASSES):
        """Run detection + tracking on one BGR frame.

        Ultralytics persists the tracker between calls when persist=True,
        so track ids stay consistent across the stream.
        """
        results = self.model.track(
            source=frame,
            persist=True,
            tracker=self.tracker_cfg,
            conf=config.CONF_THRESHOLD,
            iou=config.IOU_THRESHOLD,
            imgsz=config.IMG_SIZE,
            classes=classes,
            device=self.device,
            half=self.use_half,
            verbose=False,
        )
        return results[0]

    def close(self) -> None:
        """Release model and free GPU memory when the connection ends."""
        try:
            del self.model
        finally:
            if self.device.startswith("cuda"):
                torch.cuda.empty_cache()
