
import os
import torch
import requests
from PIL import Image
from torchvision.transforms import Compose, Resize, ToTensor, Normalize

BASE_URL = "https://github.com/darkmoorltd-jpg/GAIA/releases/download/v1.0"
MODELS_DIR = os.environ.get("MODELS_DIR", "/tmp/gaia-models")

MODEL_CONFIG = {
    "cattle":          {"url": BASE_URL + "/cattle_best_model.pt",          "num_classes": 3,  "labels": ["Foot-and-Mouth Disease", "Healthy", "Lumpy Skin Disease"], "image_size": 224},
    "poultry":         {"url": BASE_URL + "/poultry_best_model.pt",         "num_classes": 4,  "labels": ["Coccidiosis", "Healthy", "Newcastle Disease", "Salmonella"],  "image_size": 224},
    "pests_102class":  {"url": BASE_URL + "/pests_102class_best_model.pt",  "num_classes": 102, "labels": None, "image_size": 224},
    "soil_11class":    {"url": BASE_URL + "/soil_11class_best_model.pt",    "num_classes": 11, "labels": ["Alluvial", "Sandy", "Clay", "Loamy", "Laterite", "Black", "Red", "Peat", "Cinder", "Sandy Loam", "Yellow"], "image_size": 224},
    "maize":           {"url": BASE_URL + "/gaia_maize_4class.pt",          "num_classes": 4,  "labels": ["Blight", "Common_Rust", "Gray_Leaf_Spot", "Healthy"], "image_size": 224},
    "rice_10class":    {"url": BASE_URL + "/gaia_rice_10class_384px.pt",    "num_classes": 10, "labels": None, "image_size": 384},
    "millet_3class":   {"url": BASE_URL + "/gaia_millet_3class.pt",         "num_classes": 3,  "labels": ["Blast", "Rust", "Healthy"], "image_size": 224},
    "soybean_14class": {"url": BASE_URL + "/gaia_soybean_14class.pt",       "num_classes": 14, "labels": None, "image_size": 224},
    "pepper_13class":  {"url": BASE_URL + "/gaia_pepper_13class.pt",        "num_classes": 13, "labels": None, "image_size": 224},
    "cabbage_8class":  {"url": BASE_URL + "/gaia_cabbage_8class.pt",        "num_classes": 8,  "labels": None, "image_size": 224},
    "apple":           {"url": BASE_URL + "/gaia_apple.pt",                 "num_classes": 4,  "labels": ["Black Rot", "Healthy", "Rust", "Scab"], "image_size": 224},
    "cassava":         {"url": BASE_URL + "/gaia_cassava.pt",               "num_classes": 5,  "labels": ["Bacterial Blight", "Brown Streak", "Green Mottle", "Healthy", "Mosaic"], "image_size": 224},
    "coffee":          {"url": BASE_URL + "/gaia_coffee.pt",                "num_classes": 4,  "labels": ["Cercospora", "Healthy", "Red Spider Mite", "Rust"], "image_size": 224},
    "grape":           {"url": BASE_URL + "/gaia_grape.pt",                 "num_classes": 4,  "labels": ["Black Measles", "Black Rot", "Healthy", "Leaf Blight"], "image_size": 224},
    "sugarcane":       {"url": BASE_URL + "/gaia_sugarcane.pt",             "num_classes": 5,  "labels": ["Bacterial Blight", "Healthy", "Red Rot", "Red Stripe", "Rust"], "image_size": 224},
    "tea":             {"url": BASE_URL + "/gaia_tea.pt",                   "num_classes": 6,  "labels": ["Algal Leaf", "Anthracnose", "Bird Eye Spot", "Brown Blight", "Healthy", "Red Leaf Spot"], "image_size": 224},
}


def build_model_from_state_dict(state_dict, num_classes):
    from timm.models.vision_transformer import VisionTransformer
    import torch.nn as nn

    prefix = "backbone." if any(k.startswith("backbone.") for k in state_dict) else "encoder."
    embed_dim = state_dict[prefix + "cls_token"].shape[-1]
    num_patches = state_dict[prefix + "pos_embed"].shape[1] - 1
    grid = int(num_patches ** 0.5)
    img_size = grid * 16
    depth = len([k for k in state_dict if (prefix + "blocks.") in k and k.endswith(".norm1.weight")])
    num_heads = 6 if embed_dim == 384 else (12 if embed_dim == 768 else 3)

    backbone = VisionTransformer(
        img_size=img_size, patch_size=16, embed_dim=embed_dim,
        depth=depth, num_heads=num_heads, num_classes=0,
    )
    backbone_sd = {k.replace(prefix, "", 1): v for k, v in state_dict.items() if k.startswith(prefix)}
    backbone.load_state_dict(backbone_sd, strict=False)

    head_keys = sorted([k for k in state_dict if k.startswith("head.") and k.endswith(".weight")])
    if len(head_keys) == 1:
        head = nn.Linear(embed_dim, num_classes)
        head.load_state_dict({
            "weight": state_dict["head.weight"],
            "bias": state_dict["head.bias"],
        })
    else:
        layers = []
        prev = embed_dim
        for i, k in enumerate(head_keys):
            w = state_dict[k]
            b = state_dict.get(k.replace(".weight", ".bias"))
            out = w.shape[0]
            lin = nn.Linear(prev, out)
            lin.weight.data = w
            if b is not None:
                lin.bias.data = b
            layers.append(lin)
            if i < len(head_keys) - 1:
                layers.append(nn.GELU())
                layers.append(nn.Dropout(0.2))
            prev = out
        head = nn.Sequential(*layers)

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
        print("Downloading " + key + " from " + url)
        r = requests.get(url, stream=True, timeout=300)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(32768):
                if chunk:
                    f.write(chunk)
        print("Downloaded " + key + " (" + str(round(os.path.getsize(path)/1e6, 1)) + " MB)")
        return path

    def _load(self, key):
        if key in self._cache:
            return self._cache[key]
        cfg = MODEL_CONFIG[key]
        path = self._download(key, cfg["url"])
        state = torch.load(path, map_location="cpu", weights_only=False)
        model = build_model_from_state_dict(state, cfg["num_classes"])
        model.eval()
        labels = cfg["labels"]
        if labels is None:
            labels = ["Class " + str(i) for i in range(cfg["num_classes"])]
        self._cache[key] = (model, labels)
        return self._cache[key]

    def predict(self, key, img):
        model, labels = self._load(key)
        cfg = MODEL_CONFIG[key]
        img_size = cfg.get("image_size", 224)
        transform = Compose([
            Resize((img_size, img_size)),
            ToTensor(),
            Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        tensor = transform(img).unsqueeze(0)
        with torch.no_grad():
            logits = model(tensor)
            probs = torch.softmax(logits, dim=1)[0].numpy()
        preds = sorted(
            [{"label": labels[i], "confidence": float(probs[i] * 100)} for i in range(len(labels))],
            key=lambda p: -p["confidence"],
        )
        return preds[:10]
