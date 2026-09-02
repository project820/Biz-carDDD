import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from bizcard_intake import image_processing
from bizcard_intake.image_processing import CANVAS_SIZE


class ImageProcessingTest(unittest.TestCase):
    def test_profile_image_is_square_white_canvas_and_preserves_card_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "card.jpg"
            output = tmp_path / "profile" / "card_profile.jpg"

            card = Image.new("RGB", (800, 500), (235, 235, 230))
            card.save(source, format="JPEG")

            with patch.object(image_processing, "_try_vision_rectify", lambda source_path, rectified_path: None), patch.object(
                image_processing, "_try_opencv_rectify", lambda source_path, output_path: None
            ):
                prepared = image_processing.prepare_card_images(source, output)
            profile = Image.open(prepared.profile_path).convert("RGB")

            self.assertEqual(profile.size, (CANVAS_SIZE, CANVAS_SIZE))
            self.assertEqual(profile.getpixel((0, 0)), (255, 255, 255))
            self.assertTrue(prepared.ocr_path.exists())
            with Image.open(prepared.llm_path) as llm:
                self.assertLessEqual(max(llm.size), image_processing.LLM_MAX_SIDE)

            diff = ImageChops.difference(profile, Image.new("RGB", profile.size, "white"))
            bbox = diff.getbbox()
            self.assertIsNotNone(bbox)
            left, top, right, bottom = bbox
            ratio = (right - left) / (bottom - top)
            self.assertAlmostEqual(ratio, 800 / 500, delta=0.08)


if __name__ == "__main__":
    unittest.main()
