# %%
from time import time

import ciclo
import flax.linen as nn
import jax
import jax.numpy as jnp
import matplotlib.pyplot as plt
import numpy as np
import optax
import tensorflow as tf
import tensorflow_datasets as tfds
from clu.metrics import Accuracy, Average, Collection
from flax import struct
from flax.training import train_state

batch_size = 32

# load the MNIST dataset
ds_train: tf.data.Dataset = tfds.load("mnist", split="train", shuffle_files=True)
ds_train = ds_train.repeat().shuffle(1024).batch(batch_size).prefetch(1)
ds_valid: tf.data.Dataset = tfds.load("mnist", split="test")
ds_valid = ds_valid.batch(32, drop_remainder=True).prefetch(1)

# Define model
class Linear(nn.Module):
    @nn.compact
    def __call__(self, x):
        x = x / 255.0
        x = x.reshape((x.shape[0], -1))  # flatten
        x = nn.Dense(features=10)(x)
        return x


@struct.dataclass
class Metrics(Collection):
    loss: Average.from_output("loss")
    accuracy: Accuracy

    def update(self, **kwargs) -> "Metrics":
        updates = self.single_from_model_output(**kwargs)
        return self.merge(updates)


class TrainState(train_state.TrainState):
    metrics: Metrics


@jax.jit
def train_step(state: TrainState, batch):
    def loss_fn(params):
        logits = state.apply_fn({"params": params}, batch["image"])
        loss = optax.softmax_cross_entropy_with_integer_labels(
            logits=logits, labels=batch["label"]
        ).mean()
        return loss, logits

    (loss, logits), grads = jax.value_and_grad(loss_fn, has_aux=True)(state.params)
    state = state.apply_gradients(grads=grads)
    metrics = state.metrics.update(loss=loss, logits=logits, labels=batch["label"])
    return state.replace(metrics=metrics)


@jax.jit
def compute_metrics(state: TrainState):
    logs = state.metrics.compute()
    return {"stateful_metrics": logs}


@jax.jit
def eval_step(state: TrainState, batch):
    logits = state.apply_fn({"params": state.params}, batch["image"])
    loss = optax.softmax_cross_entropy_with_integer_labels(
        logits=logits, labels=batch["label"]
    ).mean()
    metrics = state.metrics.update(loss=loss, logits=logits, labels=batch["label"])
    logs = ciclo.logs()
    logs.add_metrics(metrics.compute(), stateful=True)
    return logs, state.replace(metrics=metrics)


def reset_metrics(state: TrainState, batch, _):
    return state.replace(metrics=state.metrics.empty())


# Initialize state
model = Linear()
variables = model.init(jax.random.PRNGKey(0), jnp.empty((1, 28, 28, 1)))
state = TrainState.create(
    apply_fn=model.apply,
    params=variables["params"],
    tx=optax.adamw(1e-3),
    metrics=Metrics.empty(),
)

# training loop
total_samples = 32 * 100
total_steps = total_samples // batch_size
eval_steps = total_steps // 10
log_steps = total_steps // 50
state, history, _ = ciclo.loop(
    state,
    ds_train.as_numpy_iterator(),
    {
        ciclo.every(1): train_step,
        ciclo.every(log_steps): [compute_metrics, reset_metrics],
        ciclo.every(eval_steps): [
            ciclo.inner_loop(
                "valid",
                lambda state: ciclo.loop(
                    state,
                    ds_valid.as_numpy_iterator(),
                    {ciclo.every(1): eval_step},
                    on_start=[reset_metrics],
                ),
            ),
            ciclo.checkpoint(
                f"logdir/mnist_full/{int(time())}", monitor="accuracy_valid", mode="max"
            ),
            ciclo.early_stopping(
                monitor="accuracy_valid",
                mode="max",
                patience=eval_steps * 2,
            ),
        ],
        **ciclo.keras_bar(total=total_steps),
    },
    stop=total_steps,
)

# %%

steps, loss, accuracy = history.collect("steps", "loss", "accuracy")
steps_valid, loss_valid, accuracy_valid = history.collect(
    "steps", "loss_valid", "accuracy_valid"
)

_, axs = plt.subplots(1, 2)
axs[0].plot(steps, loss, label="train")
axs[0].plot(steps_valid, loss_valid, label="valid")
axs[0].legend()
axs[0].set_title("Loss")
axs[1].plot(steps, accuracy, label="train")
axs[1].plot(steps_valid, accuracy_valid, label="valid")
axs[1].legend()
axs[1].set_title("Accuracy")
plt.show()
