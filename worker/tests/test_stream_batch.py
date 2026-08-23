from worker.stream_batch import (
    assemble_batch,
    commit_latent_buffer,
    predicted_x0,
    renoised_slot,
    shift_condition_buffer,
    split_output,
    stack_adapter_states,
)


def test_assemble_batch_prepends_new_item():
    assert assemble_batch("new", ["old"]) == ["new", "old"]
    assert assemble_batch("new", []) == ["new"]


def test_split_output_returns_last_as_finished():
    finished, prefix = split_output([1, 2, 3, 4])
    assert finished == 4
    assert prefix == [1, 2, 3]


def test_shift_condition_buffer_drops_oldest_condition():
    assert shift_condition_buffer("x", []) == []
    assert shift_condition_buffer("x", ["a", "b", "c"]) == ["x", "a", "b"]


def test_renoised_slot_respects_noise_flag():
    assert renoised_slot(10, 0.5, 0.2, 3, True) == 5.6
    assert renoised_slot(10, 0.5, 0.2, 3, False) == 5


def test_commit_latent_buffer_for_one_step():
    assert commit_latent_buffer(
        [10],
        [],
        [],
        [],
        do_add_noise=True,
    ) == (10, [])


def test_commit_latent_buffer_commits_finished_and_renoises_prefix():
    x0_batch = [10, 20, 30, 40]
    alpha_tail = [0.1, 0.2, 0.3]
    beta_tail = [0.4, 0.5, 0.6]
    noise_tail = [1, 2, 3]
    finished, buffer = commit_latent_buffer(
        x0_batch,
        alpha_tail,
        beta_tail,
        noise_tail,
        do_add_noise=True,
    )
    expected = [
        renoised_slot(x0, alpha, beta, noise, True)
        for x0, alpha, beta, noise in zip(
            x0_batch[:-1], alpha_tail, beta_tail, noise_tail
        )
    ]
    assert finished == 40
    assert len(buffer) == 3
    assert buffer == expected


def test_predicted_x0_matches_numeric_identity():
    latent = 10
    noise_pred = 3
    alpha = 0.5
    beta = 0.2
    c_skip = 0.1
    c_out = 0.9
    expected = c_out * ((latent - beta * noise_pred) / alpha) + c_skip * latent
    assert predicted_x0(
        latent, noise_pred, alpha, beta, c_skip, c_out
    ) == expected


def test_stack_adapter_states_handles_empty_and_residuals():
    assert stack_adapter_states([], tuple) == []
    assert stack_adapter_states([[1, 2], [3, 4]], tuple) == [(1, 3), (2, 4)]
