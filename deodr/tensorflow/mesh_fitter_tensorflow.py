from deodr.tensorflow import (
    Scene3DTensorflow,
    LaplacianRigidEnergyTensorflow,
    CameraTensorflow,
)
from deodr.tensorflow import TriMeshTensorflow as TriMesh
from deodr.tensorflow import ColoredTriMeshTensorflow as ColoredTriMesh
from deodr import LaplacianRigidEnergy
import numpy as np
import scipy.sparse.linalg
import scipy.spatial.transform.rotation
import copy
import tensorflow as tf


def qrot(q, v):
    qr = tf.tile(q[None, :], (v.shape[0], 1))
    qvec = qr[:, :-1]
    uv = tf.linalg.cross(qvec, v)
    uuv = tf.linalg.cross(qvec, uv)
    return v + 2 * (qr[:, 3][None, 0] * uv + uuv)


class MeshDepthFitter:
    def __init__(
        self,
        vertices,
        faces,
        euler_init,
        translation_init,
        cregu=2000,
        inertia=0.96,
        damping=0.05,
    ):
        self.cregu = cregu
        self.inertia = inertia
        self.damping = damping
        self.step_factor_vertices = 0.0005
        self.step_max_vertices = 0.5
        self.step_factor_quaternion = 0.00006
        self.step_max_quaternion = 0.1
        self.step_factor_translation = 0.00005
        self.step_max_translation = 0.1

        self.mesh = TriMesh(
            faces, vertices
        )  # we do a copy to avoid negative stride not support by Tensorflow
        object_center = vertices.mean(axis=0)
        object_radius = np.max(np.std(vertices, axis=0))
        self.camera_center = object_center + np.array([-0.5, 0, 5]) * object_radius

        self.scene = Scene3DTensorflow()
        self.scene.set_mesh(self.mesh)
        self.rigid_energy = LaplacianRigidEnergy(self.mesh, vertices, cregu)
        self.vertices_init = tf.constant(copy.copy(vertices))
        self.Hfactorized = None
        self.Hpreconditioner = None
        self.set_mesh_transform_init(euler=euler_init, translation=translation_init)
        self.reset()

    def set_mesh_transform_init(self, euler, translation):
        self.transform_quaternion_init = scipy.spatial.transform.Rotation.from_euler(
            "zyx", euler
        ).as_quat()
        self.transform_translation_init = translation

    def reset(self):
        self.vertices = copy.copy(self.vertices_init)
        self.speed_vertices = np.zeros(self.vertices_init.shape)
        self.transform_quaternion = copy.copy(self.transform_quaternion_init)
        self.transform_translation = copy.copy(self.transform_translation_init)
        self.speed_translation = np.zeros(3)
        self.speed_quaternion = np.zeros(4)

    def set_max_depth(self, max_depth):
        self.scene.max_depth = max_depth
        self.scene.set_background(
            np.full((self.height, self.width, 1), max_depth, dtype=np.float)
        )

    def set_depth_scale(self, depth_scale):
        self.depthScale = depth_scale

    def set_image(self, hand_image, focal=None, dist=None):
        self.width = hand_image.shape[1]
        self.height = hand_image.shape[0]
        assert hand_image.ndim == 2
        self.hand_image = hand_image
        if focal is None:
            focal = 2 * self.width

        rot = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]])
        trans = -rot.T.dot(self.camera_center)
        intrinsic = np.array(
            [[focal, 0, self.width / 2], [0, focal, self.height / 2], [0, 0, 1]]
        )
        extrinsic = np.column_stack((rot, trans))
        self.camera = CameraTensorflow(
            extrinsic=extrinsic,
            intrinsic=intrinsic,
            resolution=(self.width, self.height),
            dist=dist,
        )
        self.iter = 0

    def step(self):
        self.vertices = (
            self.vertices - tf.reduce_mean(self.vertices, axis=0)[None, :]
        )  # centervertices because we have another paramter to control translations

        with tf.GradientTape() as tape:

            vertices_with_grad = tf.constant(self.vertices)
            quaternion_with_grad = tf.constant(self.transform_quaternion)
            translation_with_grad = tf.constant(self.transform_translation)

            tape.watch(vertices_with_grad)
            tape.watch(quaternion_with_grad)
            tape.watch(translation_with_grad)

            vertices_with_grad_centered = (
                vertices_with_grad - tf.reduce_mean(vertices_with_grad, axis=0)[None, :]
            )

            q_normalized = quaternion_with_grad / tf.norm(
                quaternion_with_grad
            )  # that will lead to a gradient that is in the tangeant space
            vertices_with_grad_transformed = (
                qrot(q_normalized, vertices_with_grad_centered) + translation_with_grad
            )

            self.mesh.set_vertices(vertices_with_grad_transformed)

            depth_scale = 1 * self.depthScale
            depth = self.scene.render_depth(
                self.camera,
                resolution=(self.width, self.height),
                depth_scale=depth_scale,
            )
            depth = tf.clip_by_value(depth, 0, self.scene.max_depth)

            diff_image = tf.reduce_sum(
                (depth - tf.constant(self.hand_image[:, :, None])) ** 2, axis=2
            )
            loss = tf.reduce_sum(diff_image)

            trainable_variables = [
                vertices_with_grad,
                quaternion_with_grad,
                translation_with_grad,
            ]
            vertices_grad, quaternion_grad, translation_grad = tape.gradient(
                loss, trainable_variables
            )

        energy_data = loss.numpy()

        grad_data = vertices_grad

        energy_rigid, grad_rigidity, approx_hessian_rigidity = self.rigid_energy.eval(
            self.vertices.numpy()
        )
        energy = energy_data + energy_rigid
        print("Energy=%f : EData=%f E_rigid=%f" % (energy, energy_data, energy_rigid))

        # update v
        grad = grad_data + grad_rigidity

        def mult_and_clamp(x, a, t):
            return np.minimum(np.maximum(x * a, -t), t)

        # update vertices
        step_vertices = mult_and_clamp(
            -grad.numpy(), self.step_factor_vertices, self.step_max_vertices
        )
        self.speed_vertices = (1 - self.damping) * (
            self.speed_vertices * self.inertia + (1 - self.inertia) * step_vertices
        )
        self.vertices = self.vertices + self.speed_vertices
        # update rotation
        step_quaternion = mult_and_clamp(
            -quaternion_grad.numpy(),
            self.step_factor_quaternion,
            self.step_max_quaternion,
        )
        self.speed_quaternion = (1 - self.damping) * (
            self.speed_quaternion * self.inertia + (1 - self.inertia) * step_quaternion
        )
        self.transform_quaternion = self.transform_quaternion + self.speed_quaternion
        # update translation
        self.transform_quaternion = self.transform_quaternion / np.linalg.norm(
            self.transform_quaternion
        )
        step_translation = mult_and_clamp(
            -translation_grad.numpy(),
            self.step_factor_translation,
            self.step_max_translation,
        )
        self.speed_translation = (1 - self.damping) * (
            self.speed_translation * self.inertia
            + (1 - self.inertia) * step_translation
        )
        self.transform_translation = self.transform_translation + self.speed_translation

        self.iter += 1
        return energy, depth[:, :, 0].numpy(), diff_image.numpy()


class MeshRGBFitterWithPose:
    def __init__(
        self,
        vertices,
        faces,
        euler_init,
        translation_init,
        default_color,
        default_light,
        cregu=2000,
        inertia=0.96,
        damping=0.05,
        update_lights=True,
        update_color=True,
    ):
        self.cregu = cregu

        self.inertia = inertia
        self.damping = damping
        self.step_factor_vertices = 0.0005
        self.step_max_vertices = 0.5
        self.step_factor_quaternion = 0.00006
        self.step_max_quaternion = 0.05
        self.step_factor_translation = 0.00005
        self.step_max_translation = 0.1

        self.default_color = default_color
        self.default_light = default_light
        self.update_lights = update_lights
        self.update_color = update_color
        self.mesh = ColoredTriMesh(
            faces.copy()
        )  # we do a copy to avoid negative stride not support by Tensorflow
        object_center = vertices.mean(axis=0) + translation_init
        object_radius = np.max(np.std(vertices, axis=0))
        self.camera_center = object_center + np.array([0, 0, 9]) * object_radius

        self.scene = Scene3DTensorflow()
        self.scene.set_mesh(self.mesh)
        self.rigid_energy = LaplacianRigidEnergyTensorflow(self.mesh, vertices, cregu)
        self.vertices_init = tf.constant(copy.copy(vertices))
        self.Hfactorized = None
        self.Hpreconditioner = None
        self.set_mesh_transform_init(euler=euler_init, translation=translation_init)
        self.reset()

    def set_background_color(self, background_color):
        self.scene.set_background(
            np.tile(background_color[None, None, :], (self.height, self.width, 1))
        )

    def set_mesh_transform_init(self, euler, translation):
        self.transform_quaternion_init = scipy.spatial.transform.Rotation.from_euler(
            "zyx", euler
        ).as_quat()
        self.transform_translation_init = translation

    def reset(self):
        self.vertices = copy.copy(self.vertices_init)
        self.speed_vertices = np.zeros(self.vertices.shape)
        self.transform_quaternion = copy.copy(self.transform_quaternion_init)
        self.transform_translation = copy.copy(self.transform_translation_init)
        self.speed_translation = np.zeros(3)
        self.speed_quaternion = np.zeros(4)

        self.hand_color = copy.copy(self.default_color)
        self.ligth_directional = copy.copy(self.default_light["directional"])
        self.ambiant_light = copy.copy(self.default_light["ambiant"])

        self.speed_ligth_directional = np.zeros(self.ligth_directional.shape)
        self.speed_ambiant_light = np.zeros(self.ambiant_light.shape)
        self.speed_hand_color = np.zeros(self.hand_color.shape)

    def set_image(self, hand_image, focal=None, dist=None):
        self.width = hand_image.shape[1]
        self.height = hand_image.shape[0]
        assert hand_image.ndim == 3
        self.hand_image = hand_image
        if focal is None:
            focal = 2 * self.width

        rot = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]])
        trans = -rot.T.dot(self.camera_center)
        intrinsic = np.array(
            [[focal, 0, self.width / 2], [0, focal, self.height / 2], [0, 0, 1]]
        )
        extrinsic = np.column_stack((rot, trans))
        self.camera = CameraTensorflow(
            extrinsic=extrinsic,
            intrinsic=intrinsic,
            dist=dist,
            resolution=(self.width, self.height),
        )
        self.iter = 0

    def step(self):

        with tf.GradientTape() as tape:

            vertices_with_grad = tf.constant(self.vertices)
            quaternion_with_grad = tf.constant(self.transform_quaternion)
            translation_with_grad = tf.constant(self.transform_translation)

            ligth_directional_with_grad = tf.constant(self.ligth_directional)
            ambiant_light_with_grad = tf.constant(self.ambiant_light)
            hand_color_with_grad = tf.constant(self.hand_color)

            tape.watch(vertices_with_grad)
            tape.watch(quaternion_with_grad)
            tape.watch(translation_with_grad)

            tape.watch(ligth_directional_with_grad)
            tape.watch(ambiant_light_with_grad)
            tape.watch(hand_color_with_grad)

            vertices_with_grad_centered = (
                vertices_with_grad - tf.reduce_mean(vertices_with_grad, axis=0)[None, :]
            )

            q_normalized = quaternion_with_grad / tf.norm(
                quaternion_with_grad
            )  # that will lead to a gradient that is in the tangeant space
            vertices_with_grad_transformed = (
                qrot(q_normalized, vertices_with_grad_centered) + translation_with_grad
            )
            self.mesh.set_vertices(vertices_with_grad_transformed)

            self.scene.set_light(
                ligth_directional=ligth_directional_with_grad,
                ambiant_light=ambiant_light_with_grad,
            )
            self.mesh.set_vertices_colors(
                tf.tile(hand_color_with_grad[None, :], [self.mesh.nb_vertices, 1])
            )

            image = self.scene.render(self.camera)

            diff_image = tf.reduce_sum(
                (image - tf.constant(self.hand_image)) ** 2, axis=2
            )
            loss = tf.reduce_sum(diff_image)

            trainable_variables = [
                vertices_with_grad,
                quaternion_with_grad,
                translation_with_grad,
                ligth_directional_with_grad,
                ambiant_light_with_grad,
                hand_color_with_grad,
            ]
            (
                vertices_grad,
                quaternion_grad,
                translation_grad,
                ligth_directional_grad,
                ambiant_light_grad,
                hand_color_grad,
            ) = tape.gradient(loss, trainable_variables)

        energy_data = loss.numpy()

        grad_data = vertices_grad

        energy_rigid, grad_rigidity, approx_hessian_rigidity = self.rigid_energy.eval(
            self.vertices
        )
        energy = energy_data + energy_rigid.numpy()
        print("Energy=%f : EData=%f E_rigid=%f" % (energy, energy_data, energy_rigid))

        # update v
        grad = grad_data + grad_rigidity

        def mult_and_clamp(x, a, t):
            return np.minimum(np.maximum(x * a, -t), t)

        inertia = self.inertia

        # update vertices
        step_vertices = mult_and_clamp(
            -grad.numpy(), self.step_factor_vertices, self.step_max_vertices
        )
        self.speed_vertices = (1 - self.damping) * (
            self.speed_vertices * inertia + (1 - inertia) * step_vertices
        )
        self.vertices = self.vertices + self.speed_vertices
        # update rotation
        step_quaternion = mult_and_clamp(
            -quaternion_grad, self.step_factor_quaternion, self.step_max_quaternion
        )
        self.speed_quaternion = (1 - self.damping) * (
            self.speed_quaternion * inertia + (1 - inertia) * step_quaternion
        )
        self.transform_quaternion = self.transform_quaternion + self.speed_quaternion
        # update translation
        self.transform_quaternion = self.transform_quaternion / np.linalg.norm(
            self.transform_quaternion
        )
        step_translation = mult_and_clamp(
            -translation_grad, self.step_factor_translation, self.step_max_translation
        )
        self.speed_translation = (1 - self.damping) * (
            self.speed_translation * inertia + (1 - inertia) * step_translation
        )
        self.transform_translation = self.transform_translation + self.speed_translation
        # update directional light
        step = -ligth_directional_grad * 0.0001
        self.speed_ligth_directional = (1 - self.damping) * (
            self.speed_ligth_directional * inertia + (1 - inertia) * step
        )
        self.ligth_directional = self.ligth_directional + self.speed_ligth_directional
        # update ambiant light
        step = -ambiant_light_grad * 0.0001
        self.speed_ambiant_light = (1 - self.damping) * (
            self.speed_ambiant_light * inertia + (1 - inertia) * step
        )
        self.ambiant_light = self.ambiant_light + self.speed_ambiant_light
        # update hand color
        step = -hand_color_grad * 0.00001
        self.speed_hand_color = (1 - self.damping) * (
            self.speed_hand_color * inertia + (1 - inertia) * step
        )
        self.hand_color = self.hand_color + self.speed_hand_color

        self.iter += 1
        return energy, image.numpy(), diff_image.numpy()
