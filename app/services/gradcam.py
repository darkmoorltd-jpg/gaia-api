
import io
import base64
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image


class GradCAM:
    def __init__(self, model, layer_name=None):
        self.model = model
        self.model.eval()
        self.activations = None
        self.gradients = None
        self.layer_name = layer_name or self._auto_target_layer()
        self._register_hooks()

    def _auto_target_layer(self):
        for name, module in self.model.named_modules():
            if name.endswith("conv_head"):
                return name
        for name, module in self.model.named_modules():
            if "blocks" in name and name.endswith("norm1"):
                return name
        last_conv = None
        for name, module in self.model.named_modules():
            if isinstance(module, torch.nn.Conv2d):
                last_conv = name
        return last_conv

    def _register_hooks(self):
        def forward_hook(module, inp, out):
            self.activations = out.detach()

        def backward_hook(module, grad_in, grad_out):
            self.gradients = grad_out[0].detach()

        target = dict(self.model.named_modules())[self.layer_name]
        target.register_forward_hook(forward_hook)
        target.register_full_backward_hook(backward_hook)

    def generate(self, img_tensor, target_class=None):
        self.model.zero_grad()
        output = self.model(img_tensor)

        if target_class is None:
            target_class = int(output.argmax(dim=1).item())

        score = output[0, target_class]
        score.backward(retain_graph=True)

        if self.activations.dim() == 4:
            weights = self.gradients.mean(dim=(2, 3), keepdim=True)
            cam = (weights * self.activations).sum(dim=1).squeeze(0)
            cam = F.relu(cam)
        elif self.activations.dim() == 3:
            acts = self.activations[:, 1:, :]
            grads = self.gradients[:, 1:, :]
            weights = grads.mean(dim=1, keepdim=True)
            cam = (weights * acts).sum(dim=-1).squeeze(0)
            cam = F.relu(cam)
            n = cam.shape[0]
            grid = int(np.sqrt(n))
            cam = cam[: grid * grid].reshape(grid, grid)
        else:
            raise RuntimeError("Unsupported activation shape: " + str(self.activations.shape))

        cam = cam.cpu().numpy()
        cam = cam - cam.min()
        if cam.max() > 0:
            cam = cam / cam.max()
        return cam.astype(np.float32)

    def overlay(self, img_pil, cam, alpha=0.45, colormap="jet"):
        import cv2
        img = np.array(img_pil.resize((224, 224))).astype(np.uint8)
        cam_resized = cv2.resize(cam, (img.shape[1], img.shape[0]))
        cam_uint8 = np.uint8(255 * cam_resized)
        if colormap == "turbo":
            heatmap = cv2.applyColorMap(cam_uint8, cv2.COLORMAP_TURBO)
        else:
            heatmap = cv2.applyColorMap(cam_uint8, cv2.COLORMAP_JET)
        heatmap = cv2.cvtColor(heatmap, cv2.COLOR_BGR2RGB)
        overlay = (img * (1 - alpha) + heatmap * alpha).astype(np.uint8)
        return Image.fromarray(overlay)


def to_base64_png(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + b64
