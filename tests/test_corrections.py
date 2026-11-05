import pytest
from fledge.corrections import detect_correction, message_text


@pytest.mark.parametrize("message", [
    "that's wrong",
    "No, that's not what I asked for",
    "it doesn't work",
    "still broken after your change",
    "undo that please",
    "you jumbled everything up",
    "Wrong file. Use the March export",
    "nope.",
    "It thought it kicked ass but it's all wrong",
    "you got everything wrong",
])
def test_strong_corrections(message):
    found = detect_correction(message)
    assert found is not None and found.strength == "strong", message


@pytest.mark.parametrize("message", ["try again with the other API", "not quite", "close, but the dates are off",
                                     "you forgot the header row"])
def test_weak_corrections(message):
    found = detect_correction(message)
    assert found is not None and found.strength == "weak", message


@pytest.mark.parametrize("message", [
    "", "thanks!", "perfect, ship it", "no worries", "No, that's perfect", "now do the next repo",
    "Great job — works now", "can you also add a chart?", "no need to email it",
])
def test_not_corrections(message):
    assert detect_correction(message) is None, message


def test_only_the_start_of_a_long_message_counts():
    message = "Please summarise this article. " + ("filler " * 60) + "the author says it doesn't work"
    assert detect_correction(message) is None


def test_multimodal_message_text():
    parts = [{"type": "text", "text": "that's wrong"}, {"type": "image_url", "image_url": {"url": "x"}}]
    assert message_text(parts) == "that's wrong"
    assert detect_correction(parts).strength == "strong"


@pytest.mark.parametrize("message", ["That’s wrong — 02/10/2026 is 2 October", "It doesn’t work", "that‘s not right"])
def test_curly_apostrophes_from_phones_still_count(message):
    """Review finding: smart punctuation (iOS/Telegram, macOS) produced ’, which no pattern matched."""
    found = detect_correction(message)
    assert found is not None and found.strength == "strong", message


@pytest.mark.parametrize("message", ["No, thanks.", "No thank you", "Nope, that's it", "No, that’s all for now",
                                     "no, all good", "Nah, that's fine"])
def test_polite_closers_are_not_corrections(message):
    """Review finding: 'No, thanks.' to "Want me to also retry?" scored a strong correction and grounded the skill."""
    assert detect_correction(message) is None, message


@pytest.mark.parametrize("message", ["No, the dates are mixed up", "No, that's not what I asked for", "No, wrong file"])
def test_no_followed_by_a_complaint_still_counts(message):
    found = detect_correction(message)
    assert found is not None and found.strength == "strong", message
