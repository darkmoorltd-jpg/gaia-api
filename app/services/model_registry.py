import os
import torch
import requests
from PIL import Image
from torchvision.transforms import Compose, Resize, ToTensor, Normalize

BASE_URL = "https://github.com/darkmoorltd-jpg/GAIA/releases/download/v1.0"
MODELS_DIR = "/tmp/gaia-models"

MODEL_CONFIG = {
    "rice_6class": {
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

    def _download(self, key, url):
        path = os.path.join(MODELS_DIR, key + ".pt")
        if os.path.exists(path) and os.path.getsize(path) > 10000:
            return path
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(32768):
                if chunk:
                    f.write(chunk)
        return path

    def _load(self, key):
        if key in self._cache:
            return self._cache[key]
        cfg = MODEL_CONFIG[key]
        path = self._download(key, cfg["url"])
        state = torch.load(path, map_location="cpu", weights_only=False)
        state = {k: (v.float() if v.dtype == torch.float16 else v) for k, v in state.items()}
        model = build_model_from_state_dict(state, cfg["num_classes"])
        model.eval()
        self._cache[key] = (model, cfg["labels"])
        return self._cache[key]

    def predict(self, key, img):
        model, labels = self._load(key)
        cfg = MODEL_CONFIG[key]
        size = cfg.get("image_size", 224)
        tf = Compose([
            Resize((size, size)),
            ToTensor(),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        t = tf(img).unsqueeze(0)
        with torch.no_grad():
            probs = torch.softmax(model(t), dim=1)[0].numpy()
        preds = sorted(
            [{"label": labels[i], "confidence": float(probs[i] * 100)}
             for i in range(len(labels))],
            key=lambda p: -p["confidence"],
        )
        return preds[:10]

    def predict_with_cam(self, key, img):
        from app.services.gradcam import GradCAM, to_base64_png

        model, labels = self._load(key)
        cfg = MODEL_CONFIG[key]
        size = cfg.get("image_size", 224)
        tf = Compose([
            Resize((size, size)),
            ToTensor(),
            Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        original = img.resize((size, size))
        t = tf(img).unsqueeze(0)

        with torch.no_grad():
            probs = torch.softmax(model(t), dim=1)[0].numpy()

        top_idx = int(probs.argmax())
        preds = sorted(
            [{"label": labels[i], "confidence": float(probs[i] * 100)}
             for i in range(len(labels))],
            key=lambda p: -p["confidence"],
        )[:10]

        b64 = None
        try:
            eng = GradCAM(model)
            cam = eng.generate(t, target_class=top_idx)
            b64 = to_base64_png(eng.overlay(original, cam))
        except Exception as e:
            print("Grad-CAM failed: " + str(e))

        return preds, b64, top_idx
