from deodr.examples.render_mesh import render_mesh
import os
import imageio
import numpy as np


def test_render_mesh():
    obj_file = os.path.join(os.path.dirname(__file__), "models/duck.obj")
    image, channels = render_mesh(obj_file, display=False, width=320, height=240)
    image_file = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../data/test/duck.png")
    )
    image_uint8 = (image * 255).astype(np.uint8)
    image_prev = imageio.imread(image_file)
    assert np.max(np.abs(image_prev - image_uint8)) == 0
