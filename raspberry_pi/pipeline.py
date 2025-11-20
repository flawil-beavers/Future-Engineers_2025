import math
import cv2
import numpy as np

from config import ConfigLoader
from helpers import Pillar

class Pipeline:
    def __init__(self, configloader: ConfigLoader):
        self.configloader = configloader

    def undistort(self, image: np.ndarray):
        mtx, dist = np.array(self.configloader.get_property("camera")['mtx']), np.array(self.configloader.get_property("camera")['dist'])
        return cv2.undistort(image, mtx, dist, None, mtx)

    def crop(self, image: np.ndarray):
        # scale the image by 0.5 in height. This means that the image will have a height of 240 pixels instead of 480
        return cv2.resize(image, (int(image.shape[1]), int(image.shape[0] * 0.5)))
    
    def inRange(self, image: np.ndarray, min: list, max: list):
        """
        Returns a mask of the image with the pixels in the range of min and max
        """
        mins = list(min)
        maxs = list(max)
        for i in [mins, maxs]:
            i[0] = int(round(i[0]/2))
            i[1] = int(round(i[1]*2.55))
            i[2] = int(round(i[2]*2.55))
        return cv2.inRange(image, tuple(mins), tuple(maxs))

    def filter_RG_Bl(self, hsv: np.ndarray, color_image: np.ndarray):
        """
        Extracts the red, green and black colors from the image -> this is used to detect the pillars and walls
        """
        # reload config file
        self.configloader.load_config()
        redMin = list(self.configloader.get_property("filters")['REDLO'])
        redMax = list(self.configloader.get_property("filters")['REDHI'])
        greenMin = list(self.configloader.get_property("filters")['GREENLO'])
        greenMax = list(self.configloader.get_property("filters")['GREENHI'])
        grayThresh = int(self.configloader.get_property("filters")['GRAY'])


        # red filter
        # First red range
        rMask1 = self.inRange(hsv, redMin, redMax)

        # Second red range (adjusted for hue wrapping)
        redMin2 = [360 - redMax[0], redMin[1], redMin[2]]
        redMax2 = [360, redMax[1], redMax[2]]
        rMask2 = self.inRange(hsv, redMin2, redMax2)

        # Combine both red ranges
        rMask = cv2.bitwise_or(rMask1, rMask2)

        # green filter
        gMask = self.inRange(hsv, greenMin, greenMax)
    
        # blur images to remove noise
        blurredR = cv2.medianBlur(rMask, 5)
        blurredG = cv2.medianBlur(gMask, 5)

        grayImage = cv2.cvtColor(color_image, cv2.COLOR_RGB2GRAY)
        blurredImg = cv2.GaussianBlur(grayImage, (3, 3), 0)
        # edge detection
        lower = 30
        upper = 90
        blackimg = cv2.inRange(blurredImg, 0, grayThresh)
        
        ob_image = self.filter_OB(hsv)
        # combine images
        combined = cv2.bitwise_or(ob_image["orange"], ob_image["blue"])
        blackimg = cv2.subtract(blackimg, combined)
        return {"green": blurredG, "red": blurredR, "black": blackimg, "orange": ob_image["orange"], "blue": ob_image["blue"]}
        
    def filter_OB(self, hsv: np.ndarray):
        """
        Extracts the orange and blue colors from the image -> this is used to detect the turn markers
        """
        orangeMin = list(self.configloader.get_property("filters")['ORANGELO'])
        orangeMax = list(self.configloader.get_property("filters")['ORANGEHI'])
        blueMin = list(self.configloader.get_property("filters")['BLUELO'])
        blueMax = list(self.configloader.get_property("filters")['BLUEHI'])
        # orange filter
        oMask = self.inRange(hsv, orangeMin, orangeMax)
        # blue filter
        bMask = self.inRange(hsv, blueMin, blueMax)
        # blur images to remove noise
        blurredO = cv2.medianBlur(oMask, 5)
        blurredB = cv2.medianBlur(bMask, 5)
        # remove more noise by eroding and dilating the image
        kernel_erode = np.ones((3, 3), np.uint8)
        kernel_dilate = np.ones((10, 10), np.uint8)
        blurredO = cv2.erode(blurredO, kernel_erode, iterations=2)
        blurredO = cv2.dilate(blurredO, kernel_dilate, iterations=2)
        blurredB = cv2.erode(blurredB, kernel_erode, iterations=2)
        blurredB = cv2.dilate(blurredB, kernel_dilate, iterations=2)
        return {"orange": blurredO, "blue": blurredB}
    
    def filter_pink(self, hsv: np.ndarray):
        pinkMin = list(self.configloader.get_property("filters")['PINKLO'])
        pinkMax = list(self.configloader.get_property("filters")['PINKHI'])
        pMask = self.inRange(hsv, pinkMin, pinkMax)
        blurredP = cv2.medianBlur(pMask, 5)
        return blurredP

    def get_pillars(self, imgIn: np.ndarray, type="RED") -> list[Pillar]:
        """
        Extracts pillars from filtered image.
        """
        minSize = float(self.configloader.get_property("contours")['minSize'])
        edges = cv2.Canny(cv2.medianBlur(cv2.copyMakeBorder(imgIn[:], 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0), 3), 30, 200)

        contours, hierarchy = cv2.findContours(edges, 
            cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        processedContours = []
        for contour in contours:
            size = cv2.contourArea(contour)
            if size > minSize:
                moment = cv2.moments(contour)
                if moment["m00"] != 0:
                    x = int(moment["m10"] / moment["m00"])
                    y = int(moment["m01"] / moment["m00"])

                    # Get bounding rectangle
                    rect_x, rect_y, w, h = cv2.boundingRect(contour)
                    width = math.ceil(w)
                    height = math.ceil(h)

                    # Calculate the y-coordinate of the lowest part of the pillar
                    lowest_y = rect_y + h

                    # Filter out invalid contours
                    if height * width <= 40.0 or height < width * 1.1:
                        continue

                    # Pass the lowest_y to the Pillar object
                    processedContours.append(Pillar(x, width, height, type, y=lowest_y))
        return processedContours
    
    def process_pillars(self, state, straight_sections):
        """
        Process detected pillars and update the straight sections with pillar information.
        Args:
            state (SharedState): The shared state containing detected pillars and other information.
            straight_sections (list): A list of Straight_Section objects representing the straight sections.
        """
        # Filter pillars
        
        detected_pillars_r = self.get_pillars(state.latest_streams["red"], "RED")
        detected_pillars_g = self.get_pillars(state.latest_streams["green"], "GREEN")
        state.detected_pillars = detected_pillars_r + detected_pillars_g

        state.detected_pillars.sort(key=lambda x: x.width * x.height, reverse=True)
        first_section = (state.rounds == 0)
        section_index = state.rounds % 4

        # If we drove one round, just return driving pos
        if state.rounds > 4:
            return straight_sections[section_index].calculate_driving_pos()

        # Reset parking lot pillar state
        if straight_sections[section_index].parking_lot:
            print(f"parking lot in section, resetting pillars")
            for i in range(3):
                straight_sections[section_index].l[i] = 0
                straight_sections[section_index].r[i] = 0
        if first_section:
            cv2.line(state.latest_streams["viz"], (0, 50), (640, 50), (0, 255, 255), 1)
            cv2.line(state.latest_streams["viz"], (0, 23), (640, 23), (255, 255, 0), 1)
            opening_top = 40
            opening_bottom = 200
            cv2.line(state.latest_streams["viz"], (320-opening_bottom, 240), (320-opening_top, 0), (255, 255, 0), 1)
            cv2.line(state.latest_streams["viz"], (320+opening_bottom, 240), (320+opening_top, 0), (255, 255, 0), 1)
        else:
            cv2.line(state.latest_streams["viz"], (0, 50), (640, 50), (0, 255, 255), 1)
            cv2.line(state.latest_streams["viz"], (0, 22), (640, 22), (255, 255, 0), 1)
            cv2.line(state.latest_streams["viz"], (0, 11), (640, 11), (255, 0, 255), 1)
            opening_top = 40
            opening_bottom = 200
            cv2.line(state.latest_streams["viz"], (320-opening_bottom, 240), (320-opening_top, 0), (255, 255, 0), 1)
            cv2.line(state.latest_streams["viz"], (320+opening_bottom, 240), (320+opening_top, 0), (255, 255, 0), 1)
        # Process pillars
        for p in state.detected_pillars: # todo show where the undetected pillars are
            p.x = p.screen_x - 320
            index = None
            # Ignored pillars (wrong size/ratio/etc.)
            cv2.circle(state.latest_streams["viz"], (p.screen_x, p.y), 3, (255, 0, 0), -1)
            if p.ignore:
                cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y - p.height),
                            (p.screen_x + int(p.width*0.35), p.y),
                            ((0, 0, 50) if p.color == "RED" else (0, 50, 0)), 3)
                cv2.putText(state.latest_streams["viz"], f"{p.color} {int(p.y)} {index}",
                            (p.screen_x - int(p.width*0.35), p.y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                continue
            if first_section:
                if p.y > 130:
                    print(f"--Pillar {p.color} too near (>130), y={p.y}")
                if abs(p.screen_x - 320) > 200:
                    print(f"--Pillar {p.color} too far from center (>200), dx={abs(p.screen_x - 320)}")

                if p.y > 50:
                    index = 0
                elif p.y > 23:
                    index = 1
                else:
                    print(f"--Pillar {p.color} too far (<24), y={p.y}")

            else:
                if p.y > 150:
                    print(f"--Pillar {p.color} too near (>150), y={p.y}")

                if p.y > 50:
                    index = 0
                elif p.y > 22:
                    index = 1
                elif p.y > 11:
                    index = 2
                else:
                    print(f"--Pillar {p.color} too far (<11), y={p.y}")

            # Draw rejected pillar
            if index is None:
                cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y - p.height),
                            (p.screen_x + int(p.width*0.35), p.y),
                            ((0, 0, 100) if p.color == "RED" else (0, 100, 0)), 1)
                cv2.putText(state.latest_streams["viz"], f"{p.color[0]} ({int(p.x)},{int(p.y)}) {index}",
                            (p.screen_x - int(p.width*0.35), p.y + 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                continue

            # Draw accepted pillar
            cv2.rectangle(state.latest_streams["viz"], (p.screen_x - int(p.width*0.35), p.y - p.height),
                        (p.screen_x + int(p.width*0.35), p.y),
                        ((0, 0, 255) if p.color == "RED" else (0, 255, 0)), 1)
            cv2.putText(state.latest_streams["viz"], f"{p.color[0]} ({int(p.x)},{int(p.y)}) {index}",
                        (p.screen_x - int(p.width*0.35), p.y + 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            print(f"Pillar {p.color} accepted at index {index}, y={p.y}, x={p.screen_x}, section {section_index}")

            # Update left/right pillar storage
            if p.screen_x < 320:
                straight_sections[section_index].l[index] = p.color
            else:
                straight_sections[section_index].r[index] = p.color

        # Mark parking lot for section 0
        straight_sections[section_index].parking_lot = (section_index == 0)

        # Validate and save images
        straight_sections[section_index].validate(state.round_dir)
        cv2.imwrite(f"logs/image{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["color_image"])
        cv2.imwrite(f"logs/image_viz{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["viz"])
        cv2.imwrite(f"logs/image_red{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["red"])
        cv2.imwrite(f"logs/image_green{section_index}{'_p' if first_section else ''}.jpg", state.latest_streams["green"])

        pillar_driving_pos = straight_sections[section_index].calculate_driving_pos()
        straight_sections[section_index].print()
        return pillar_driving_pos