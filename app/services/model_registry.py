import os
import io
import torch
import requests
import numpy as np
from PIL import Image
from torchvision.transforms import Compose, Resize, ToTensor, Normalize

BASE_URL = "https://github.com/darkmoorltd-jpg/GAIA/releases/download/v1.0"
ONNX_BASE = "https://github.com/darkmoorltd-jpg/gaia-mobile/releases/download"
MODELS_DIR = "/tmp/gaia-models"

MODEL_CONFIG = {
    "cattle": {
        "type": "pytorch",
        "url": BASE_URL + "/gaia_cattle.pt",
        "num_classes": 3,
        "labels": ["Foot-and-Mouth Disease", "Healthy", "Lumpy Skin Disease"],
        "image_size": 224,
    },
    "maize": {
        "type": "pytorch",
        "url": BASE_URL + "/gaia_maize_4class.pt",
        "num_classes": 4,
        "labels": ["Blight", "Common_Rust", "Gray_Leaf_Spot", "Healthy"],
        "image_size": 224,
    },
    "rice_6class": {
        "type": "pytorch",
        "url": BASE_URL + "/gaia_rice_6class.pt",
        "num_classes": 6,
        "labels": [
            "Bacterial Leaf Blight",
            "Brown Spot",
            "Healthy Rice Leaf",
            "Leaf Blast",
            "Leaf scald",
            "Sheath Blight",
        ],
        "image_size": 224,
    },
    "pests_102class": {
        "type": "onnx",
        "url": ONNX_BASE + "/models-v2/pests_102class_v2.onnx",
        "labels_url": ONNX_BASE + "/models-v2/pests_102_labels.json",
        "num_classes": 102,
        "image_size": 224,
    },
    "cotton": {
        "type": "onnx",
        "url": ONNX_BASE + "/models-v2/cotton_effnetb0.onnx",
        "labels_url": ONNX_BASE + "/models-v2/cotton_labels.json",
        "num_classes": 8,
        "image_size": 224,
    },
    "groundnut": {
        "type": "onnx",
        "url": ONNX_BASE + "/models-v2/groundnut_effnetb0.onnx",
        "labels_url": ONNX_BASE + "/models-v2/groundnut_labels.json",
        "num_classes": 5,
        "image_size": 224,
    },
    "millet": {
        "type": "onnx",
        "url": ONNX_BASE + "/models-v2/millet_effnetb0.onnx",
        "labels_url": ONNX_BASE + "/models-v2/millet_labels.json",
        "num_classes": 3,
        "image_size": 224,
    },
}


def build_model_from_state_dict(state_dict, num_classes):
    import torch.nn as nn
    import timm

    backbone = timm.create_model("efficientnet_b0", pretrained=False, num_classes=0)

    bb_sd = {}
    for k, v in state_dict.items():
        if k.startswith("backbone."):
            bb_sd[k.replace("backbone.", "", 1)] = v
    bb_sd = {k: v for k, v in bb_sd.items() if not k.startswith("classifier.")}
    backbone.load_state_dict(bb_sd, strict=False)

    head_sd = {}
    for k, v in state_dict.items():
        if k.startswith("head."):
            head_sd[k.replace("head.", "", 1)] = v

    wkeys = sorted([k for k in head_sd if k.endswith(".weight")])
    if wkeys:
        last = wkeys[-1]
        w = head_sd[last]
        b = head_sd.get(last.replace(".weight", ".bias"))
        lin = nn.Linear(w.shape[1], w.shape[0])
        lin.weight.data = w
        if b is not None:
            lin.bias.data = b
        head = lin
    else:
        head = nn.Linear(backbone.num_features, num_classes)

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.head = head

        def forward(self, x):
            return self.head(self.backbone(x))

    return Net()


class _ONNXPredictor:
    """Wraps an ONNX model + labels + preprocessing."""
    def __init__(self, model_path, labels, image_size):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.labels = labels
        self.image_size = image_size
        self.tf = Compose([
            Resize(int(image_size * 1.14)),
            Resize((image_size, image_size)),  # force exact 224x224
            ToTensor(),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])

    def predict(self, img):
        x = self.tf(img).unsqueeze(0).numpy().astype(np.float32)
        out = self.sess.run(None, {"input": x})[0][0]
        e = np.exp(out - out.max())
        probs = e / e.sum()
        top = np.argsort(probs)[::-1][:5]
        return [
            {"label": self.labels[i] if i < len(self.labels) else f"Class {i}",
             "confidence": float(probs[i] * 100)}
            for i in top
        ]


class ModelRegistry:
    def __init__(self):
        self._cache = {}
        os.makedirs(MODELS_DIR, exist_ok=True)

    def has(self, key):
        return key in MODEL_CONFIG

    def list_models(self):
        return list(MODEL_CONFIG.keys())

    def loaded_keys(self):
        return list(self._cache.keys())

    def _download(self, url, dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 10000:
            return dest
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return dest

    def _load_pytorch(self, key, cfg):
        path = os.path.join(MODELS_DIR, key + ".pt")
        if not (os.path.exists(path) and os.path.getsize(path) > 10000):
            self._download(cfg["url"], path)
        state = torch.load(path, map_location="cpu", weights_only=False)
        model = build_model_from_state_dict(state, cfg["num_classes"])
        model.eval()
        return model

    def _load_onnx(self, key, cfg):
        onnx_path = os.path.join(MODELS_DIR, key + ".onnx")
        if not (os.path.exists(onnx_path) and os.path.getsize(onnx_path) > 10000):
            self._download(cfg["url"], onnx_path)

        labels_path = os.path.join(MODELS_DIR, key + "_labels.json")
        if not (os.path.exists(labels_path) and os.path.getsize(labels_path) > 10):
            self._download(cfg["labels_url"], labels_path)
        import json
        labels = json.load(open(labels_path))["classes"]

        return _ONNXPredictor(onnx_path, labels, cfg["image_size"])

    def _get(self, key):
        if key in self._cache:
            return self._cache[key]
        cfg = MODEL_CONFIG[key]
        if cfg.get("type") == "onnx":
            obj = self._load_onnx(key, cfg)
        else:
            obj = self._load_pytorch(key, cfg)
        self._cache[key] = obj
        return obj

    def predict(self, key, img):
        obj = self._get(key)
        cfg = MODEL_CONFIG[key]

        if cfg.get("type") == "onnx":
            return obj.predict(img)

        # PyTorch path
        tf = Compose([
            Resize((cfg["image_size"], cfg["image_size"])),
            ToTensor(),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        x = tf(img).unsqueeze(0)
        with torch.no_grad():
            logits = obj(x)
            probs = torch.nn.functional.softmax(logits, dim=1)[0]
        top5 = torch.argsort(probs, descending=True)[:5]
        return [
            {"label": cfg["labels"][int(i)] if int(i) < len(cfg["labels"]) else f"Class {int(i)}",
             "confidence": float(probs[int(i)].item() * 100)}
            for i in top5
        ]
