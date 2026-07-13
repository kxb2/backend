from unittest.mock import Mock

import pytest

from app.generations.service import (
    PromptValidationError,
    apply_integrated_prompt,
    split_shots,
    validate_prompt_length,
)
from app.generations.models import Cut
from app.storyboards.models import Storyboard


def _shot_text(order_no: int) -> str:
    return f"description for shot {order_no}"


def _integrated_prompt(order_nos: list[int] | None = None) -> str:
    return "\n".join(f"Shot {n}: {_shot_text(n)}" for n in (order_nos or list(range(1, 10))))


def _storyboard_with_cuts(order_nos: list[int] | None = None) -> Storyboard:
    storyboard = Storyboard(id=1)
    storyboard.cuts = [Cut(order_no=n) for n in (order_nos or list(range(1, 10)))]
    return storyboard


class TestSplitShots:
    def test_splits_nine_shots_in_order(self):
        shots = split_shots(_integrated_prompt())

        assert len(shots) == 9
        for n in range(1, 10):
            assert shots[n] == _shot_text(n)

    def test_maps_correctly_even_when_shots_appear_out_of_order(self):
        shuffled = [3, 1, 2, 4, 5, 6, 7, 8, 9]
        shots = split_shots(_integrated_prompt(shuffled))

        assert shots[3] == _shot_text(3)
        assert shots[1] == _shot_text(1)

    def test_raises_when_fewer_than_nine_shots(self):
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt(list(range(1, 9))))

    def test_raises_when_more_than_nine_shots(self):
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt(list(range(1, 11))))

    def test_raises_when_shot_numbers_are_not_exactly_one_to_nine(self):
        # 1이 중복되고 5가 빠짐
        with pytest.raises(PromptValidationError):
            split_shots(_integrated_prompt([1, 1, 2, 3, 4, 6, 7, 8, 9]))

    def test_raises_when_a_shot_is_empty(self):
        prompt = _integrated_prompt(list(range(1, 9))) + "\nShot 9:   "

        with pytest.raises(PromptValidationError):
            split_shots(prompt)


class TestValidatePromptLength:
    def test_passes_under_limit(self):
        validate_prompt_length("x" * 2999)

    def test_passes_exactly_at_limit(self):
        validate_prompt_length("x" * 3000)

    def test_raises_over_limit(self):
        with pytest.raises(PromptValidationError):
            validate_prompt_length("x" * 3001)


class TestApplyIntegratedPrompt:
    def test_assigns_prompt_to_storyboard_and_each_cut(self):
        storyboard = _storyboard_with_cuts()
        db = Mock()
        prompt = _integrated_prompt()

        apply_integrated_prompt(db, storyboard, prompt)

        assert storyboard.integrated_prompt == prompt
        for cut in storyboard.cuts:
            assert cut.prompt_text == _shot_text(cut.order_no)
        db.commit.assert_called_once()

    def test_raises_and_does_not_commit_when_cut_count_is_wrong(self):
        storyboard = _storyboard_with_cuts(list(range(1, 9)))  # 8개뿐
        db = Mock()

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, _integrated_prompt())

        db.commit.assert_not_called()

    def test_raises_and_does_not_commit_when_prompt_too_long(self):
        storyboard = _storyboard_with_cuts()
        db = Mock()
        too_long = _integrated_prompt() + "x" * 3000

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, too_long)

        db.commit.assert_not_called()
        assert storyboard.integrated_prompt is None

    def test_raises_and_does_not_commit_when_shots_malformed(self):
        storyboard = _storyboard_with_cuts()
        db = Mock()

        with pytest.raises(PromptValidationError):
            apply_integrated_prompt(db, storyboard, "not a valid integrated prompt")

        db.commit.assert_not_called()
        assert storyboard.integrated_prompt is None
