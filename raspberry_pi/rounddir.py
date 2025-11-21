import cv2
import numpy as np
from helpers import extract_ROI

def find_round_dir(state, is_pillar_round: bool = False):
    """
    Determine the direction of the current track round based on the black line image.

    There are two modes:
    1. `is_pillar_round=True`: Uses the left and right halves of the top portion of the black image 
       to compare intensity and infer the direction.
    2. `is_pillar_round=False`: Uses edge detection and wall height analysis to infer the track direction.

    Args:
        state (SharedState): The shared state object containing the latest image streams, including:
            - state.latest_streams["black"]: Grayscale image with black walls/lines highlighted.
        is_pillar_round (bool, optional): Flag indicating whether this is a pillar detection round.
            Defaults to False.

    Returns:
        int: Direction of the round:
            -1 -> clockwise (CW)
             1 -> counter-clockwise (CCW)
    """
    black_img = state.latest_streams["black"]

    if is_pillar_round:
        # For pillar rounds, analyze the left and right top portions of the black image
        left = extract_ROI(black_img, [0, 0], [100, black_img.shape[0]//2])
        right = extract_ROI(black_img, [black_img.shape[1] - 100, 0], [black_img.shape[1], black_img.shape[0]//2])
        
        if left.sum() < right.sum():
            print("rounddir: ccw")
            return 1  # counter-clockwise
        else:
            print("rounddir: cw")
            return -1  # clockwise

    else: # todo: use canny edge detection to find the gap
        # For normal rounds, detect edges of walls and analyze height differences
        lower = 30
        upper = 90
        edges_img = cv2.Canny(black_img, lower, upper, 3)

        # Ensure the top and bottom rows do not interfere with floor detection
        edges_img = cv2.line(edges_img, (0, 0), (edges_img.shape[1], 0), 1, 1)
        edges_img = cv2.line(edges_img, (0, edges_img.shape[0]-1), (edges_img.shape[1], edges_img.shape[0]-1), 1, 1)

        # Find the first edge (top-most pixel) in each column
        wall_heights = np.argmax(edges_img, axis=0)

        # If no edge detected, set to bottom row
        wall_heights = ((wall_heights == 0) * (edges_img.shape[0]-1)) + wall_heights

        # Compute differences between consecutive columns to detect jumps
        differences = np.diff(wall_heights)

        # Count jumps above threshold to determine direction
        MIN_JUMP = 9  # minimum vertical difference to consider a wall jump
        counter_clockwise = np.sum(differences > MIN_JUMP)
        clockwise = np.sum(differences < -MIN_JUMP)

        print("rounddir:", clockwise > counter_clockwise)
        print("cw ", clockwise)
        print("ccw", counter_clockwise)

        # If more clockwise jumps, the round direction is clockwise (-1), else counter-clockwise (1)
        return 1 if clockwise > counter_clockwise else -1
