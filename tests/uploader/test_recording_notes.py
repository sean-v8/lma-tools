import unittest

from lma_tools.uploader.core import parse_notes
from lma_tools.uploader.notes import normalize_source, parse_recording_section

ID = "artist2026-08-17.audience.t-flac2448"


def notes(section):
    return (
        "Artist\nAugust 17, 2026\nVenue\nExample City, ST\n\n"
        + section
        + "\nExample Taper <taper@example.com>\n\n"
        "01. Example Song\n"
    )


class RecordingNotesTests(unittest.TestCase):
    def check_case(self, section, source, lineage):
        text = notes(section)
        metadata = parse_notes(text, ID)
        self.assertEqual(metadata["source"], source)
        self.assertEqual(metadata["lineage"], lineage)
        self.assertEqual(metadata["description"], text.rstrip("\n"))
        self.assertEqual(metadata["transferer"], "Example Taper")
        self.assertNotIn("setlist", metadata)

    def test_single_pair_with_recorder_in_lineage(self):
        self.check_case(
            "AKG 391 - 8' NOS, slight LOC, 15' from stage\n\n"
            "> Zoom F3 > WAV (32/48) > Audacity > WAV (24/48) > FLAC",
            "AKG 391 (8' NOS, slight LOC, 15' from stage)",
            "Zoom F3 > WAV (32/48) > Audacity > WAV (24/48) > FLAC",
        )

    def test_two_numbered_recorders_and_spot_sources(self):
        self.check_case(
            "Line Audio Omni1 - overhead, 5' split\n> Zoom F6 #1\n\n"
            "Aguilar Tone Hammer - DI bass\nBeyerdynamic TG D71 - kick\n"
            "Electro-Voice 635A - drums\nNord Stage 3 - DI stereo keyboards\n"
            "Sennheiser e906 - guitar\n> Zoom F6 #2\n\n"
            "> WAV (32/48) > Audacity > WAV (24/48) > FLAC",
            "[Line Audio Omni1 (overhead, 5' split) + Multitrack]",
            "Zoom F6 x2 > WAV (32/48) > Audacity > WAV (24/48) > FLAC",
        )

    def test_microphone_branch_and_console_branch(self):
        self.check_case(
            "Line Audio CM4 - 3' center stage NOS\n"
            "Line Audio Omni1 - 2' stage lip, 8' split\n> Zoom F6 > WAV "
            "(32/48)\n\n"
            "Allen & Heath SQ-5 Multitrack\n> Ardour > RF64 (24/48)\n\n"
            "> Audacity > WAV (24/48) > FLAC",
            "[Line Audio CM4 (3' center stage NOS) + Line Audio Omni1 (2' "
            "stage lip, 8' split) > Zoom F6]"
            " + [Allen & Heath SQ-5 Multitrack > Ardour > RF64 (24/48)]",
            "Audacity > WAV (24/48) > FLAC",
        )

    def test_one_recorder_overheads_and_spot_sources(self):
        self.check_case(
            "Lauten LA-120 - overhead XY\nBeyerdynamic TG D71 - kick\n"
            "Electro-Voice 635A - drums\nBugera BV1001M - DI bass\n"
            "Sennheiser e609 - guitar amp\n\n"
            "> Zoom F6 > WAV (32/48) > Audacity > WAV (24/48) > FLAC",
            "Lauten LA-120 (overhead XY) + Multitrack",
            "Zoom F6 > WAV (32/48) > Audacity > WAV (24/48) > FLAC",
        )

    def test_unknown_sources_stay_visible(self):
        self.check_case(
            "Microphone A - placement undecided\nMystery device - auxiliary "
            "signal\n\n"
            "> Recorder > WAV (24/48) > FLAC",
            "Microphone A (placement undecided) + Mystery device (auxiliary "
            "signal)",
            "Recorder > WAV (24/48) > FLAC",
        )

    def test_explicit_room_hint_takes_precedence_over_instrument(self):
        self.check_case(
            "Microphone A - drums overhead XY\nInput B - DI bass\n\n"
            "> Recorder > WAV (24/48) > FLAC",
            "Microphone A (drums overhead XY) + Multitrack",
            "Recorder > WAV (24/48) > FLAC",
        )

    def test_single_di_source_is_not_hidden(self):
        self.check_case(
            "Input A - DI bass\n\n> Recorder > WAV (24/48) > FLAC",
            "Input A (DI bass)",
            "Recorder > WAV (24/48) > FLAC",
        )

    def test_two_different_recorders_keep_their_branches(self):
        self.check_case(
            "Pair A - room\n> Recorder A #1\n\nPair B - room\n> Recorder B "
            "#2\n\n"
            "> WAV (24/48) > FLAC",
            "[Pair A (room) > Recorder A #1] + [Pair B (room) > Recorder B "
            "#2]",
            "WAV (24/48) > FLAC",
        )

    def test_unlabelled_recorder_not_assumed_to_be_an_additional_unit(self):
        self.check_case(
            "Pair A - room\n> Recorder #1\n\nPair B - room\n> Recorder\n\n"
            "> WAV (24/48) > FLAC",
            "[Pair A (room) > Recorder #1] + [Pair B (room) > Recorder]",
            "WAV (24/48) > FLAC",
        )

    def test_software_branch_keeps_its_capture_format(self):
        self.check_case(
            "Pair A - room\n> Recorder > WAV (32/48)\n\n"
            "Digital feed\n> Ardour > WAV (24/48)\n\n> Editor > FLAC",
            "[Pair A (room) > Recorder] + [Digital feed > Ardour > WAV "
            "(24/48)]",
            "Editor > FLAC",
        )

    def test_missing_blank_before_final_processing(self):
        self.check_case(
            "Pair A - room\n> Recorder > WAV (24/48) > FLAC",
            "Pair A (room)",
            "Recorder > WAV (24/48) > FLAC",
        )

    def test_existing_parentheses_and_equipment_hyphens_preserved(self):
        self.assertEqual(
            normalize_source("Model-123 (room)"), "Model-123 (room)"
        )
        self.assertEqual(
            normalize_source("Model-123 - room"), "Model-123 (room)"
        )
        self.assertEqual(
            normalize_source("Model-123 — room"), "Model-123 (room)"
        )

    def test_existing_inline_layout_is_preserved(self):
        self.check_case(
            "Pair A (room) > Recorder A\nPair B (room) > Recorder B\n\n"
            "> WAV (24/48) > FLAC",
            "[Pair A (room) > Recorder A] + [Pair B (room) > Recorder B]",
            "WAV (24/48) > FLAC",
        )

    def test_processing_chain_spans_multiple_lines(self):
        self.check_case(
            "Pair A - room\n\n> Recorder > WAV (32/48)\n"
            "> Editor > WAV (24/48) > FLAC",
            "Pair A (room)",
            "Recorder > WAV (32/48) > Editor > WAV (24/48) > FLAC",
        )

    def test_missing_processing_or_source_does_not_invent_metadata(self):
        for lines in [[], ["Pair A - room"], ["> WAV (24/48) > FLAC"]]:
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                parse_recording_section(lines)


if __name__ == "__main__":
    unittest.main()
