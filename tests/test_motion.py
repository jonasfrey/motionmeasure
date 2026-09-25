import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from process_video import detect


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def video(self, moving, fps=29.97, count=240):
        path = self.root / 'fixture.avi'
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (320, 180))
        self.assertTrue(writer.isOpened())
        for i in range(count):
            frame = np.full((180, 320, 3), 35, np.uint8)
            if moving and (35 <= i < 90 or 150 <= i < 215):
                x = 20 + (i * 4) % 240
                cv2.rectangle(frame, (x, 60), (x+35, 105), (240, 240, 240), -1)
            writer.write(frame)
        writer.release()
        return path

    def test_motion_json_boxes_and_fractional_playback_rate(self):
        source = self.video(True)
        result = detect(source, self.root, step=5, min_area=80, min_frames=2, min_duration=.3, gap=.3)
        self.assertEqual(result['segment_count'], 2)
        self.assertLess(abs(result['segments'][0]['start'] - 35/29.97), .2)
        self.assertLess(abs(result['segments'][1]['start'] - 150/29.97), .2)
        for sample in result['samples']:
            self.assertAlmostEqual(sample['timestamp'], sample['frame']/result['src_fps'], places=5)
            for box in sample['boxes']:
                self.assertTrue(0 <= box['x'] < 320)
                self.assertLessEqual(box['x'] + box['width'], 320)
                self.assertLessEqual(box['y'] + box['height'], 180)
        disk = json.loads((self.root/'fixture.events.json').read_text())
        self.assertEqual(disk, result)
        self.assertNotIn('_last_t', disk['segments'][0])
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_format', '-of', 'json', str(self.root/result['annotated'])]))
        self.assertLess(abs(float(info['format']['duration']) - result['duration']), .01)

    def test_static_video_and_short_event_filter(self):
        result = detect(self.video(False), self.root, min_area=80)
        self.assertEqual(result['samples'], [])
        self.assertEqual(result['segments'], [])
        result = detect(self.video(True), self.root, min_area=80, min_duration=20)
        self.assertTrue(result['samples'])
        self.assertEqual(result['segments'], [])

    def test_invalid_input_and_settings(self):
        with self.assertRaises(ValueError):
            detect(self.root/'missing.mp4', self.root)
        self.assertEqual(json.loads((self.root/'missing.status.json').read_text())['state'], 'error')
        with self.assertRaises(ValueError):
            detect('unused', self.root, step=0)


if __name__ == '__main__':
    unittest.main()
