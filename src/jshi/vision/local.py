from io import BytesIO


class ChangeDetector:
    """Cheap pixel-change baseline, never claims object semantics."""
    def __init__(self):
        self.previous = None

    def detect(self, data):
        from PIL import Image, ImageChops, ImageStat
        with Image.open(BytesIO(data)) as im:
            small = im.convert('L').resize((64, 64))
        change = 1.0 if self.previous is None else ImageStat.Stat(ImageChops.difference(small, self.previous)).mean[0] / 255
        self.previous = small
        return [], change > .10


class YoloDetector:
    def __init__(self, weights):
        from ultralytics import YOLO
        self.model = YOLO(weights)
        self.previous = None

    def detect(self, data):
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            result = self.model.track(im.convert('RGB'), persist=True, verbose=False, tracker='bytetrack.yaml', device='cpu')[0]
        detections = []
        for box in result.boxes:
            cls = int(box.cls[0])
            xy = [round(float(v), 3) for v in box.xyxyn[0]]
            detections.append({"class": result.names[cls], "box": xy,
                "confidence": round(float(box.conf[0]), 3),
                "track": int(box.id[0]) if box.id is not None else None})
        # Object-class counts and rough position changes are signals, not scene descriptions.
        signature = sorted((d['class'], d['track'] or 0, *(round(v * 5) for v in d['box'])) for d in detections)
        changed = self.previous is not None and signature != self.previous
        self.previous = signature
        return detections, changed
