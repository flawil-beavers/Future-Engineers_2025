import math
import cv2
import numpy as np

from config import ConfigLoader
from helpers import Pillar

class Pipeline:
    """
    Image processing pipeline for detecting colored pillars, walls, and markers.

    Attributes:
        configloader (ConfigLoader): Instance of ConfigLoader for loading filters and parameters.
    """

    def __init__(self, configloader: ConfigLoader):
        """
        Initialize the Pipeline with a configuration loader.

        Args:
            configloader (ConfigLoader): Provides access to configuration parameters.
        """
        self.configloader = configloader

    def undistort(self, image: np.ndarray) -> np.ndarray:
        """
        Correct lens distortion using camera matrix and distortion coefficients.

        Args:
            image (np.ndarray): Input distorted image.

        Returns:
            np.ndarray: Undistorted image.
        """
        mtx, dist = np.array(self.configloader.get_property("camera")['mtx']), np.array(self.configloader.get_property("camera")['dist'])
        return cv2.undistort(image, mtx, dist, None, mtx)

    def crop(self, image: np.ndarray) -> np.ndarray:
        """
        Crop the image by scaling its height to 50%.

        Args:
            image (np.ndarray): Input image.

        Returns:
            np.ndarray: Cropped/resized image.
        """
        return cv2.resize(image, (int(image.shape[1]), int(image.shape[0] * 0.5)))
    
    def inRange(self, image: np.ndarray, min: list, max: list) -> np.ndarray:
        """
        Create a mask of pixels within a specified HSV range.

        Args:
            image (np.ndarray): Input HSV image.
            min (list): Minimum HSV values.
            max (list): Maximum HSV values.

        Returns:
            np.ndarray: Binary mask where pixels within range are 255.
        """
        mins = list(min)
        maxs = list(max)
        for i in [mins, maxs]:
            i[0] = int(round(i[0]/2))
            i[1] = int(round(i[1]*2.55))
            i[2] = int(round(i[2]*2.55))
        return cv2.inRange(image, tuple(mins), tuple(maxs))

    def filter_RG_Bl(self, hsv: np.ndarray, color_image: np.ndarray, calibrate: bool) -> dict:
        """
        Extract red, green, black, orange, and blue colors from the image.

        Args:
            hsv (np.ndarray): HSV image.
            color_image (np.ndarray): Original RGB image.

        Returns:
            dict: Masks for each color, e.g. {"green": mask, "red": mask, ...}.
        """
        # Avoid reloading the config file on every frame — this caused
        # excessive disk IO and slowed down per-frame processing.
        if calibrate:
            self.configloader.load_config()
        redMin = list(self.configloader.get_property("filters")['REDLO'])
        redMax = list(self.configloader.get_property("filters")['REDHI'])
        greenMin = list(self.configloader.get_property("filters")['GREENLO'])
        greenMax = list(self.configloader.get_property("filters")['GREENHI'])
        grayThresh = int(self.configloader.get_property("filters")['GRAY'])

        # Red filter (two ranges to handle hue wrap-around)
        rMask1 = self.inRange(hsv, redMin, redMax)
        redMin2 = [360 - redMax[0], redMin[1], redMin[2]]
        redMax2 = [360, redMax[1], redMax[2]]
        rMask2 = self.inRange(hsv, redMin2, redMax2)
        rMask = cv2.bitwise_or(rMask1, rMask2)

        # Green filter
        gMask = self.inRange(hsv, greenMin, greenMax)

        # Blur to remove noise
        blurredR = cv2.medianBlur(rMask, 5)
        blurredG = cv2.medianBlur(gMask, 5)

        # Black filter via grayscale and thresholding
        grayImage = cv2.cvtColor(color_image, cv2.COLOR_RGB2GRAY)
        blurredImg = cv2.GaussianBlur(grayImage, (3, 3), 0)
        blackimg = cv2.inRange(blurredImg, 0, grayThresh)
        
        # Orange/blue detection and removal from black mask
        ob_image = self.filter_OB(hsv)
        combined = cv2.bitwise_or(ob_image["orange"], ob_image["blue"])
        blackimg = cv2.subtract(blackimg, combined)

        return {"green": blurredG, "red": blurredR, "black": blackimg, "orange": ob_image["orange"], "blue": ob_image["blue"]}
        
    def filter_OB(self, hsv: np.ndarray) -> dict:
        """
        Extract orange and blue colors for turn markers.

        Args:
            hsv (np.ndarray): HSV image.

        Returns:
            dict: {"orange": orange_mask, "blue": blue_mask}
        """
        orangeMin = list(self.configloader.get_property("filters")['ORANGELO'])
        orangeMax = list(self.configloader.get_property("filters")['ORANGEHI'])
        blueMin = list(self.configloader.get_property("filters")['BLUELO'])
        blueMax = list(self.configloader.get_property("filters")['BLUEHI'])

        oMask = self.inRange(hsv, orangeMin, orangeMax)
        bMask = self.inRange(hsv, blueMin, blueMax)

        # Blur to remove noise
        blurredO = cv2.medianBlur(oMask, 5)
        blurredB = cv2.medianBlur(bMask, 5)

        # Erode and dilate for further noise removal
        kernel_erode = np.ones((3, 3), np.uint8)
        kernel_dilate = np.ones((10, 10), np.uint8)
        blurredO = cv2.erode(blurredO, kernel_erode, iterations=2)
        blurredO = cv2.dilate(blurredO, kernel_dilate, iterations=2)
        blurredB = cv2.erode(blurredB, kernel_erode, iterations=2)
        blurredB = cv2.dilate(blurredB, kernel_dilate, iterations=2)

        return {"orange": blurredO, "blue": blurredB}
    
    def filter_pink(self, hsv: np.ndarray) -> np.ndarray:
        """
        Extract pink regions from the image.

        Args:
            hsv (np.ndarray): HSV image.

        Returns:
            np.ndarray: Pink color mask.
        """
        pinkMin = list(self.configloader.get_property("filters")['PINKLO'])
        pinkMax = list(self.configloader.get_property("filters")['PINKHI'])
        pMask = self.inRange(hsv, pinkMin, pinkMax)
        blurredP = cv2.medianBlur(pMask, 5)
        return blurredP

    def get_pillars(self, imgIn: np.ndarray, type="RED") -> list[Pillar]:
        """
        Detect pillar objects from a binary mask.

        Args:
            imgIn (np.ndarray): Mask of a specific color (RED or GREEN).
            type (str): Color type for the pillars.

        Returns:
            list[Pillar]: List of Pillar objects with position, size, and color.
        """
        minSize = float(self.configloader.get_property("contours")['minSize'])
        edges = cv2.Canny(cv2.medianBlur(cv2.copyMakeBorder(imgIn[:], 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0), 3), 30, 200)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        processedContours = []
        for contour in contours:
            size = cv2.contourArea(contour)
            if size > minSize:
                moment = cv2.moments(contour)
                if moment["m00"] != 0:
                    x = int(moment["m10"] / moment["m00"])
                    y = int(moment["m01"] / moment["m00"])
                    rect_x, rect_y, w, h = cv2.boundingRect(contour)
                    width = math.ceil(w)
                    height = math.ceil(h)
                    lowest_y = rect_y + h
                    if height * width <= 40.0 or (height < width * 1.1 and lowest_y > 30):
                        continue
                    processedContours.append(Pillar(x, width, height, type, y=lowest_y))
        return processedContours

    def process_pillars(self, state, straight_sections):
        """
        Analyze detected pillars, classify them into track sections, and update
        the corresponding `Straight_Section` with left/right pillar information.

        This method performs the following pipeline:

        1. **Detect pillars**
        - Extract red and green pillars from thresholded images (`state.latest_streams`).
        - Combine and sort pillars by size (width * height) in descending order.

        2. **Determine current section**
        - Section index is `state.rounds % 4`.
        - First section (`rounds == 0`) uses different height thresholds.

        3. **Reset parking-lot sections**
        - If `straight_sections[section_index].parking_lot` is active,
            the existing pillar classification is cleared.

        4. **Draw visualization guides**
        - Draw y-threshold horizontal lines.
        - Draw trapezoid-shaped left/right boundaries that determine pillar validity.

        5. **Iterate through detected pillars**
        For each pillar:
        - Ignore pillars marked with `p.ignore`.
        - Reject pillars outside trapezoid boundaries.
        - Assign a distance index (0/1/2) depending on the Y-position thresholds.
        - Draw the pillar bounding box + status tag.
        - Store the accepted pillar color (RED/GREEN) into either:
            - `straight_sections[i].l[index]`
            - `straight_sections[i].r[index]`
            depending on which side of the image it's on.

        6. **Mark parking lot**
        - Section 0 is always flagged as part of the parking lot.

        7. **Validate section**
        - Calls section.validate() using the current driving direction.

        8. **Save logs**
        - Saves raw images and visualization frames to `logs/`.

        9. **Compute driving position**
        - Returns the calculated driving position from the updated section.

        Args:
            state (SharedState):
                A structure containing:
                - latest_streams (dict): processed frames ("red", "green", "viz", "color_image")
                - rounds (int): number of track laps completed
                - round_dir (str): direction of travel
                - detected_pillars (list[Pillar]): updated with detected pillars

            straight_sections (list[Straight_Section]):
                List of straight-track segments, each storing pillar data for distance indices.

        Returns:
            list[str]: The driving position classification determined for the current section.
        """
        first_section   = (state.rounds == 0)
        section_index   = state.rounds % 4

        # --- Skip classification after completing a full lap ---
        if state.rounds > 4:
            return straight_sections[section_index].calculate_driving_pos()

        # --- Detect pillars from red/green masks ---
        detected_pillars_r = self.get_pillars(state.latest_streams["red"], "RED")
        detected_pillars_g = self.get_pillars(state.latest_streams["green"], "GREEN")
        state.detected_pillars = detected_pillars_r + detected_pillars_g
        state.detected_pillars.sort(key=lambda x: x.width * x.height, reverse=True)


        # --- Reset sections that were part of the parking lot ---
        if straight_sections[section_index].parking_lot:
            print("parking lot in section, resetting pillars")
            for i in range(3):
                straight_sections[section_index].l[i] = 0
                straight_sections[section_index].r[i] = 0


        # --- Set visualization thresholds depending on lap section ---
        if first_section:
            y_lines = [(50, (0, 255, 255)), (23, (255, 255, 0))]
            opening_top = 40
            opening_bottom = 200
        else:
            y_lines = [(50, (0, 255, 255)), (18, (255, 255, 0)), (11, (255, 0, 255))]
            opening_top = 45
            opening_bottom = 350

        # Draw Y-threshold lines
        for y, col in y_lines:
            cv2.line(state.latest_streams["viz"], (0, y), (640, y), col, 1)

        # Draw trapezoid boundaries
        cv2.line(state.latest_streams["viz"], (320 - opening_bottom, 240),
                (320 - opening_top, 0), (255, 255, 0), 1)

        cv2.line(state.latest_streams["viz"], (320 + opening_bottom, 240),
                (320 + opening_top, 0), (255, 255, 0), 1)


        # --- Classify each pillar ---
        for p in state.detected_pillars:
            p.x = p.screen_x - 320  # Horizontal offset from center

            # Draw raw detection point
            cv2.circle(state.latest_streams["viz"], (p.screen_x, p.y), 3, (255, 0, 0), -1)

            # Step 1: Skip ignored pillars
            if p.ignore:
                self.draw_pillar(state.latest_streams["viz"], p,
                                color=(0, 0, 50) if p.color == "RED" else (0, 50, 0),
                                tag="IGN")
                continue

            # Step 2: Trapezoid gating (height-dependent width)
            t = p.y / 240.0
            horizontal_offset = opening_top + (opening_bottom - opening_top) * t
            left_bound  = 320 - horizontal_offset
            right_bound = 320 + horizontal_offset

            # Draw gating markers
            cv2.circle(state.latest_streams["viz"], (int(left_bound), p.y), 2, (255, 200, 0), -1)
            cv2.circle(state.latest_streams["viz"], (int(right_bound), p.y), 2, (255, 200, 0), -1)

            # Reject pillars outside the trapezoid mask
            if not (left_bound <= p.screen_x <= right_bound):
                self.draw_pillar(state.latest_streams["viz"], p,
                                color=(50, 50, 50), tag="OUT")
                continue

            # Step 3: Distance index classification by Y position
            index = None
            if first_section:
                if p.y > 50: index = 0
                elif p.y > 23: index = 1
            else:
                if p.y > 50: index = 0
                elif p.y > 18: index = 1
                elif p.y > 11: index = 2

            # Reject if index invalid
            if index is None:
                self.draw_pillar(state.latest_streams["viz"], p,
                                color=(0, 0, 100) if p.color == "RED" else (0, 100, 0),
                                tag="?")
                continue

            # Accepted pillar
            self.draw_pillar(state.latest_streams["viz"], p,
                            color=(0, 0, 255) if p.color == "RED" else (0, 255, 0),
                            tag=str(index)[0])
            print(f"Pillar {p.color} accepted at index {index}, y={p.y}, "
                f"x={p.screen_x}, section {section_index}")

            # Assign pillar to left or right track side
            side_list = (straight_sections[section_index].l
                        if p.screen_x < 320
                        else straight_sections[section_index].r)
            side_list[index] = p.color


        # --- Mark parking lot for section 0 ---
        straight_sections[section_index].parking_lot = (section_index == 0)

        # --- Validate and save logs ---
        straight_sections[section_index].validate(state.round_dir)
        cv2.imwrite(f"logs/image{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["color_image"])
        cv2.imwrite(f"logs/image_viz{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["viz"])
        cv2.imwrite(f"logs/image_red{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["red"])
        cv2.imwrite(f"logs/image_green{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["green"])

        # --- Return driving position ---
        straight_sections[section_index].print()
        return straight_sections[section_index].calculate_driving_pos()


    def draw_pillar(self, viz, p, color, tag):
        """
        Draw a bounding rectangle and label for a detected pillar on a visualization frame.

        This helper is used to annotate pillars in the diagnostic image ("viz")
        with both their bounding box and a classification tag.

        Args:
            viz (np.ndarray): Visualization image (BGR).
            p (Pillar): The pillar object containing:
                - screen_x (int): x-coordinate on screen
                - y (int): bottom y-coordinate of pillar
                - width (int): pillar width
                - height (int): pillar height
                - color (str): "RED" or "GREEN"
                - x (float): horizontal offset from image center
            color (tuple[int,int,int]): BGR color for the rectangle.
            tag (str): Short text label describing pillar state
                    (e.g., "0", "1", "IGN", "OUT", "?").
        """
        cv2.rectangle(
            viz,
            (p.screen_x - int(p.width * 0.35), p.y - p.height),
            (p.screen_x + int(p.width * 0.35), p.y),
            color,
            1
        )
        cv2.putText(
            viz,
            f"{p.color[0]}({int(p.x)},{int(p.y)}) {tag}",
            (p.screen_x - int(p.width * 0.35), p.y + 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (255, 255, 255),
            1
        )
