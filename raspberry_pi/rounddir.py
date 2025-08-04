import cv2
import numpy as np
from helpers import extract_ROI


def find_round_dir(black_img: np.ndarray, is_pillar_round: bool = False):
    if is_pillar_round:
        left = extract_ROI(black_img, [0, 0], [100, black_img.shape[0]//2])
        right = extract_ROI(black_img, [black_img.shape[1] - 100, 0], [black_img.shape[1], black_img.shape[0]//2])
        if (left.sum() < right.sum()):
            print("rounddir: ccw")
            return -1
        else:
            print("rounddir: cw")
            return 1
    else:
        lower = 30
        upper = 90
        edges_img = cv2.Canny(black_img, lower, upper, 3)
        # make the bottom row all white, so we don't detect the floor
        edges_img = cv2.line(edges_img, (0, 0), (edges_img.shape[1], 0), 1, 1)
        edges_img = cv2.line(edges_img, (0, edges_img.shape[0]-1), (edges_img.shape[1], edges_img.shape[0]-1), 1, 1)
        # print(edges_img.shape)

        wall_heights = np.argmax(edges_img, axis=0)

        wall_heights = ((wall_heights == 0) * edges_img.shape[1]-1) + wall_heights

        # print("wh", wall_heights)

        differences = np.diff(wall_heights)
        # print("diff", differences)


        # count the number of positive and negative jumps in the differences
        # if there are more positive jumps, we're going counter-clockwise
        # if there are more negative jumps, we're going clockwise
        # jumps need to be at least 17 pixels high
        MIN_JUMP = 9
        counter_clockwise = np.sum(differences > MIN_JUMP)
        clockwise = np.sum(differences < -MIN_JUMP)
        # print("cc", counter_clockwise)
        # print("cw", clockwise)
        print("rounddir:", clockwise > counter_clockwise)
        print("cw ", clockwise)
        print("ccw", counter_clockwise)
        return -1 if clockwise > counter_clockwise else 1
