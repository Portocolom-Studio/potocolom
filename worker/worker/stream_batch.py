from __future__ import annotations


def assemble_batch(new_item, buffer: list) -> list:
    return [new_item, *buffer]


def split_output(batch: list) -> tuple:
    return batch[-1], batch[:-1]


def shift_condition_buffer(new_item, buffer: list) -> list:
    if not buffer:
        return []
    return [new_item, *buffer[:-1]]


def renoised_slot(x0, alpha, beta, noise, do_add_noise: bool):
    scaled = alpha * x0
    if do_add_noise:
        return scaled + beta * noise
    return scaled


def commit_latent_buffer(
    x0_batch: list,
    alpha_tail: list,
    beta_tail: list,
    noise_tail: list,
    *,
    do_add_noise: bool,
) -> tuple:
    finished, prefix = split_output(x0_batch)
    buffer = [
        renoised_slot(x0, a, b, n, do_add_noise)
        for x0, a, b, n in zip(prefix, alpha_tail, beta_tail, noise_tail)
    ]
    return finished, buffer


def predicted_x0(latent, noise_pred, alpha, beta, c_skip, c_out):
    f_theta = (latent - beta * noise_pred) / alpha
    return c_out * f_theta + c_skip * latent


def stack_adapter_states(states: list, cat) -> list:
    if not states:
        return []
    width = len(states[0])
    return [cat([state[index] for state in states]) for index in range(width)]
