import io
import base64
import numpy as np
from PIL import Image


def generate_gradcam_base64(model, input_tensor, target_class_idx, image_size=224):
    """Simple gradient-weighted CAM for ViT."""
    try:
        activations = []
        gradients = []

        def fwd_hook(m, inp, out):
            activations.append(out)

        def bwd_hook(m, gin, gout):
            gradients.append(gout[0])

        target_layer = None
        for name, module in model.named_modules():
            if name.endswith("norm2"):
                target_layer = module

        if target_layer is None:
            return None

        h1 = target_layer.register_forward_hook(fwd_hook)
        h2 = target_layer.register_full_backward_hook(bwd_hook)

        model.zero_grad()
        logits = model(input_tensor)
        score = logits[0, target_class_idx]
        score.backward()

        h1.remove()
        h2.remove()

        if not activations or not gradients:
            return None

        act = activations[0]
        grad = gradients[0]

        # Act shape: (B, N, D) or (B, N+1, D)
        if act.shape[1] > 1:
            # Drop CLS token if present
            act = act[:, 1:, :]
            grad = grad[:, 1:, :]

        weights = grad.mean(dim=1, keepdim=True)
        cam = (weights * act).sum(dim=-1)[0].detach().cpu().numpy()

        num_patches = cam.shape[0]
        grid = int(num_patches ** 0.5)
        cam = cam[: grid * grid].reshape(grid, grid)
        cam = np.maximum(cam, 0)
        if cam.max() > 0:
            cam = cam / cam.max()

        cam_img = Image.fromarray((cam * 255).astype(np.uint8))
        cam_img = cam_img.resize((image_size, image_size), Image.BILINEAR)

        buf = io.BytesIO()
        cam_img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None
