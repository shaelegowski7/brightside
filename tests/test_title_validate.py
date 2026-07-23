from app.matching.title_validate import titles_plausibly_match


def test_brand_and_word_overlap_passes():
    assert titles_plausibly_match(
        "Bose QuietComfort 45 Over-Ear Wireless Headphones - White",
        "Bose QuietComfort 45 Bluetooth Wireless Noise Cancelling Headphones",
    ) is True


def test_single_word_overlap_rejects():
    """Only one shared word -- not enough to trust, even if it's the brand."""
    assert titles_plausibly_match(
        "Bose Travel Bag Case",
        "Bose QuietComfort 45 Bluetooth Wireless Noise Cancelling Headphones",
    ) is False


def test_two_word_overlap_passes_even_without_brand_word():
    """Real live case (2026-07-23): a correct EAN match got false-positive
    rejected under the old "brand word must overlap" rule because Amazon's
    title omits the brand (common on beauty listings) -- deal title says
    "Lattafa Oud Najdia...", Amazon's title is just "Oud Najdia Eau de
    Parfum". Two distinctive overlapping words ("oud", "najdia") is enough
    to trust without requiring the specific brand token."""
    assert titles_plausibly_match(
        "119° - Lattafa Oud Najdia Eau De Parfum 100ml",
        "Oud Najdia Eau de Parfum",
    ) is True


def test_zero_overlap_rejects_doom_webcam_case():
    assert titles_plausibly_match(
        "DOOM Eternal PS4 Game",
        "Logitech C920 HD Pro Webcam",
    ) is False


def test_missing_amazon_title_passes_through():
    """No independent signal to validate against -- don't block an
    otherwise-successful match just because Keepa didn't return a title."""
    assert titles_plausibly_match("Forge Steel Drill AF300UK Deal", None) is True
    assert titles_plausibly_match("Forge Steel Drill AF300UK Deal", "") is True
