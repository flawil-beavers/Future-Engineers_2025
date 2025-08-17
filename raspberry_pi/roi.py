#!/usr/bin/env python3
import asyncio
import base64
import json
import math
import os
from time import sleep
import cv2
import numpy as np
import serial
import serial.tools.list_ports
from websockets import WebSocketServerProtocol, serve
from config import ConfigLoader
from helpers import Pillar, extract_ROI, print_past_time, Straight_Section, Lines, bound, setup_logging, connect_arduino
from pipeline import Pipeline
from statemachine import StateMachine
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera
import sys

#?: Webviewer controls for tuning colors, pd, and selecting stream

# todo: move most of this code to the main function

# turn the camera image by 180 degrees

configloader = ConfigLoader("config.json")
pipeline = Pipeline(configloader)

picam2 = Picamera2()
# picam2.configure(picam2.create_preview_configuration())
preview_config = picam2.create_preview_configuration(main={"size": (640, 480)})
preview_config["transform"] = libcamera.Transform(vflip=True, hflip=True)
picam2.configure(preview_config)
picam2.start()

picam2.set_controls({
    "AwbEnable": True,
    "ScalerCrop": [
        0,
        1520,
        4065,
        1520
    ],
    # controls.AWB_TEMPERATURE: fixed_temperature
})

roi_center_w, roi_center_h = 100, 50
roi_center_x, roi_center_y = 320 - roi_center_w // 2, 180

roi_width = 100
roi_height = 150

current_streams = ["viz", "roi_left", "roi_right", "roi_center", "red", "green", "black", "orange", "blue", "hsv_image", "color_image"]
has_sent_streams_info = False
active_websocket = None
# Shared dictionary to hold the latest streams from cycle()
latest_streams = {}


ports = serial.tools.list_ports.comports()

# get some frames so camera can adjust
for i in range(30):
    picam2.capture_array()

try:
    ser = serial.Serial(configloader.get_property("ArduinoSerialPort"), configloader.get_property("ArduinoBaudRate"))
except:
    print("Arduino not connected, available devices")
    ser = None
    for port, desc, hwid in sorted(ports):
        print("{}: {} [{}]".format(port, desc, hwid))

# Load the config file
with open("config.json", "r") as f:
    config = json.load(f)

last_error = 0.0

# create a list with four elements of Straight_Section
straight_sections = [Straight_Section(i) for i in range(4)]

kp = configloader.get_property("PD")['kp']
kd = configloader.get_property("PD")['kd']


def pause_robot():
    print("Robot paused")
    while ser and not ser.readline().decode('utf-8').strip() == "enable 1":
        sleep(0.1)
    print("Robot resumed")

def read_arduino():
    """
    Communicates with an Arduino device to retrieve distance and gyro heading data.

    This function sends a command to the Arduino to request sensor readings. It reads the distance and angle values,
    handling special "enable 0" messages to pause the robot if necessary. The retrieved values are then updated in the
    state manager.

    Globals:
        ser: Serial connection object to the Arduino.
        sm: State manager object for updating distance and angle.
        calibrate: Boolean flag indicating calibration mode.

    Side Effects:
        - May pause the robot if "enable 0" is received from the Arduino.
        - Updates the state manager with new distance and angle values.

    Returns:
        None
    """
    global ser, calibrate
    distance = 0.0
    angle = 0.0
    # Send z to the Arduino to get the distance and gyro heading
    if ser and not calibrate: # ! inefficient
        if ser.in_waiting > 0 and ser.readline().decode('utf-8').strip() == "enable 0":
            pause_robot()
        message = "z\n"
        ser.write(message.encode())
        # read the distance from the Arduino
        distance = ser.readline().decode('utf-8').strip()
        if distance == "enable 0":
            pause_robot()
            ser.write(message.encode())
            distance = ser.readline().decode('utf-8').strip()
        distance = float(distance)
        # read the gyro heading from the Arduino
        angle = ser.readline().decode('utf-8').strip()
        if angle == "enable 0":
            pause_robot()
            ser.write(message.encode())
            angle = ser.readline().decode('utf-8').strip()
        angle = float(angle)
    # sm.update_distance(distance) # takes all together 20-35 ms
    # sm.update_angle(angle)

def process_image(picam2, pipeline):
    """
    Captures an image from the camera, crops it, converts it to HSV, and extracts regions of interest (ROIs)
    for turn marker detection and PD control.

    Args:
        picam2 (Picamera2): The camera object to capture images.
        pipeline (Pipeline): The pipeline object for cropping and color filtering.

    Returns:
        dict: A dictionary containing the processed images and ROIs:
            - color_image: Cropped color image.
            - viz: Copy of the cropped color image for visualization.
            - hsv_image: HSV converted image.
            - roi_center: Center ROI for turn marker detection.
            - rgbl: Dictionary of filtered color masks.
            - roi_left_side: Left side ROI for PD control.
            - roi_right_side: Right side ROI for PD control.
            - roi_left: Cropped left ROI for PD control.
            - roi_right: Cropped right ROI for PD control.
            - portion_black_l: Portion of black in the left ROI.
            - portion_black_r: Portion of black in the right ROI.
    """
    img = cv2.cvtColor(picam2.capture_array(), cv2.COLOR_RGB2BGR) # 5-10ms
    color_image = pipeline.crop(img)
    viz = color_image.copy()
    hsv_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)

    roi_center = extract_ROI(hsv_image, [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])

    rgbl = pipeline.filter_RG_Bl(hsv_image, color_image)

    roi_left_side = extract_ROI(rgbl["black"], [0, 0], [roi_width, rgbl["black"].shape[0]])
    roi_right_side = extract_ROI(rgbl["black"], [640-roi_width, 0], [640, rgbl["black"].shape[0]])

    roi_left = extract_ROI(roi_left_side, [0, 0], [roi_width, roi_height])
    roi_right = extract_ROI(roi_right_side, [0, 0], [roi_width, roi_height])

    portion_black_l = cv2.countNonZero(roi_left) / (roi_left.shape[0] * roi_left.shape[1])
    portion_black_r = cv2.countNonZero(roi_right) / (roi_right.shape[0] * roi_right.shape[1])

    return {
        "color_image": color_image,
        "viz": viz,
        "hsv_image": hsv_image,
        "roi_center": roi_center,
        "rgbl": rgbl,
        "roi_left_side": roi_left_side,
        "roi_right_side": roi_right_side,
        "roi_left": roi_left,
        "roi_right": roi_right,
        "portion_black_l": portion_black_l,
        "portion_black_r": portion_black_r
    }

def detect_edge_lines(roi_left_side, roi_right_side, roi_width, headless, viz) -> dict:
    """
    Detects edge lines in the left and right side ROIs using Canny edge detection and Hough line transform.
    Draws detected lines and their properties on the visualization image if not in headless mode.

    Args:
        roi_left_side (np.ndarray): Left side ROI image (mask).
        roi_right_side (np.ndarray): Right side ROI image (mask).
        roi_width (int): Width of the ROI.
        headless (bool): If True, disables visualization drawing.
        viz (np.ndarray): Visualization image to draw lines and annotations on.

    Returns:
        dict: Dictionary with keys 'L' and 'R' containing detected lines for left and right sides as Lines objects.
    """
    roi_lines = {"L": [], "R": []}
    for image, key in zip([roi_left_side, roi_right_side], ["L", "R"]):
        # remove the 5 uppest rows from the image just in case the robot sees over the barriers
        image = image[5:, :]
        blurredImg = cv2.GaussianBlur(image, (3, 3), 0)
        lower = 30
        upper = 90
        edges_img = cv2.Canny(blurredImg, lower, upper, 3)
        roi_lines[key] = cv2.HoughLinesP(edges_img, 1, np.pi/180, 10, minLineLength=25, maxLineGap=50)
    border_lines = {"L": Lines(roi_lines["L"], (0, 5), (0, 0)), "R": Lines(roi_lines["R"], (640-roi_width, 5), (roi_width, 0))}

    if not headless:
        for line_group in border_lines.values():
            if line_group is not None:
                b = 200
                for line in line_group.lines:
                    cv2.line(viz, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 
                        (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), (b, 100, 0), 2)
                    cv2.putText(viz, f"{line['x1'] + line['x_offset']} {line['y1'] + line['y_offset']} {line['x2'] + line['x_offset']} {line['y2'] + line['y_offset']}", 
                        (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    # create a green dot for x1 y1 and a red dot for x2 y2
                    cv2.circle(viz, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 3, (0, 255, 0), -1)
                    cv2.circle(viz, (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), 3, (0, 0, 255), -1)
                    if b == 200:
                        cv2.putText(viz, f"y={line['m']:.2f}x+{line['b']:.2f}", 
                            (line["x1"] + line["x_offset"], 200 + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    b *= 0.6
    return border_lines



def cycle():
    global last_error, straight_sections, angle_following, latest_streams

    read_arduino()

    processed_images = process_image(picam2, pipeline)

    # todo: some of these variables can be removed
    color_image = processed_images["color_image"]
    viz = processed_images["viz"]
    hsv_image = processed_images["hsv_image"]
    roi_center = processed_images["roi_center"]
    rgbl = processed_images["rgbl"]
    roi_left_side = processed_images["roi_left_side"]
    roi_right_side = processed_images["roi_right_side"]
    roi_left = processed_images["roi_left"]
    roi_right = processed_images["roi_right"]
    portion_black_l = processed_images["portion_black_l"]
    portion_black_r = processed_images["portion_black_r"]

    if pillars:
        border_lines = detect_edge_lines(roi_left_side, roi_right_side, roi_width, headless, viz)

        roi_front_width = 10
        roi_front = extract_ROI(rgbl["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
        portion_black_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])
        distance_front = portion_black_front
        # sm.distance_front = portion_black_front
        if not headless:
            cv2.rectangle(viz, (640//2-roi_front_width, 0), (640//2+roi_front_width, 140), (255, 0, 0), 3)
            cv2.putText(viz, f"{portion_black_front:.2f}", (300, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(viz, f"{portion_black_l:.2f}", (110, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(viz, f"{portion_black_r:.2f}", (510, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            # cv2.putText(viz, f"o: {portion_orange:.2f}", (300, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            # cv2.putText(viz, f"b: {portion_blue:.2f}", (300, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

        # filter out the red and green colors of the pillars and walls
        detected_pillars_r = pipeline.get_pillars(rgbl["red"], "RED")
        detected_pillars_g = pipeline.get_pillars(rgbl["green"], "GREEN")
        detected_pillars = detected_pillars_r + detected_pillars_g

        detected_pillars.sort(key=lambda x: x.width*x.height, reverse=True)
        
        for i in ["L", "R"]: # todo use this information
            lines = border_lines[i].lines
            # calculate the slope and intercept of the line
            detected_corner = None
            for line in lines:
                # detect if any line forms a corner with the first line
                max_diff = 400 # max difference squared of the two points to be the same point
                different_slopes = line["m"] != 0 and lines[0]["m"] != 0 and line["m"]/abs(line["m"]) != lines[0]["m"]/abs(lines[0]["m"])
                if (line["x2"] - lines[0]["x1"]) ** 2 + (line["y2"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x2"] + lines[0]["x1"]) // 2, (line["y2"] + lines[0]["y1"]) // 2, lines.index(line), "different")
                    break
                elif (line["x1"] - lines[0]["x2"]) ** 2 + (line["y1"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x1"] + lines[0]["x2"]) // 2, (line["y1"] + lines[0]["y2"]) // 2, lines.index(line), "different")
                    break
                elif (line["x2"] - lines[0]["x2"]) ** 2 + (line["y2"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x2"] + lines[0]["x2"]) // 2, (line["y2"] + lines[0]["y2"]) // 2, lines.index(line), "same")
                    break
                elif (line["x1"] - lines[0]["x1"]) ** 2 + (line["y1"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x1"] + lines[0]["x1"]) // 2, (line["y1"] + lines[0]["y1"]) // 2, lines.index(line), "same")
                    break
            if detected_corner != None:
                # if not headless:
                # Determine the correct x position for the detected corner based on round_dir
                if i == "L":
                    corner_x = detected_corner[0]
                else:
                    corner_x = 640 + detected_corner[0]
                cv2.circle(viz, (corner_x, detected_corner[1]), 5, (255, 255 if detected_corner[3] == "different" else 100, 0), -1)
                cv2.putText(viz, f"{detected_corner[0]} {detected_corner[1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)


    # A state machine is used to model the car's behavior
    # This checks if the car should transition to a new state, and if so, transitions
    # states may be PD-CENTER, PD-RIGHT, PD-LEFT, TURNING-L, TURNING-R, etc.
    if not calibrate:
        if not pillars:
            orange_roi = extract_ROI(rgbl["orange"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
            blue_roi = extract_ROI(rgbl["blue"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
            portion_orange = cv2.countNonZero(orange_roi) / (orange_roi.shape[0] * orange_roi.shape[1])
            portion_blue = cv2.countNonZero(blue_roi) / (blue_roi.shape[0] * blue_roi.shape[1])

    # PD control

    # This is the reference value for the single side PD control, 
    # eg. how much black should be on the left side when the car follows the left outer wall

    # pillar_ref = 0.35
    # if sm.next_pillar:
    #   if sm.next_pillar.ignore:
    #     pillar_ref = 0.48
    

    # REF_PORTION = 0.45 if not sm.isPillarRound else 0.30
    # REF_PORTION_SIDE = 0.8

    # if "PD-CENTER-" in sm.current_state:
    #     REF_PORTION = 0.4
    
    # # error value
    # error = 0.0

    # turn_correction = 0.75 if not sm.isPillarRound else 1.0

    # PD_STATES = ["PD-CENTER", "PD-CENTER-START", "PD-CENTER-PARKING-1", "PD-CENTER-PARKING-2"]

    # # follow the left wall, if we're going counter-clockwise
    # if sm.current_state in PD_STATES and sm.round_dir == -1:
    #     error = (REF_PORTION - portion_black_r) * 1.2

    # # follow the right wall, if we're going clockwise
    # if sm.current_state in PD_STATES and sm.round_dir == 1:
    #     error = (portion_black_l - REF_PORTION) * 1.2
    
    # if pillars and sm.side != None:
    #     side = sm.side
    #     if sm.following_angle == True:
    #         error = -sm.diff_angle / 80
    #     elif sm.round_dir == -1 and portion_black_r > 0.99 and "INNER" in side:
    #         # if the right side is black, we should follow the left wall
    #         error = REF_PORTION - portion_black_r
    #     elif sm.round_dir == 1 and portion_black_l > 0.99 and "INNER" in side:
    #         # if the left side is black, we should follow the right wall
    #         error = portion_black_l - REF_PORTION
    #     elif len(border_lines[side[-1]].lines) > 0:
    #         # if there are lines in the image, we should follow them      
    #         lines = border_lines[side[-1]].lines
    #         # calculate the slope and intercept of the line
    #         slope = lines[0]["m"]
    #         intercept = lines[0]["b"]
    #         detected_corner = None
    #         for line in lines:
    #             # detect if any line forms a corner with the first line
    #             max_diff = 400 # max difference squared of the two points to be the same point
    #             different_slopes = line["m"] != 0 and lines[0]["m"] != 0 and line["m"]/abs(line["m"]) != lines[0]["m"]/abs(lines[0]["m"])
    #             if (line["x2"] - lines[0]["x1"]) ** 2 + (line["y2"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
    #                 detected_corner = ((line["x2"] + lines[0]["x1"]) // 2, (line["y2"] + lines[0]["y1"]) // 2, lines.index(line), "different")
    #                 break
    #             elif (line["x1"] - lines[0]["x2"]) ** 2 + (line["y1"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
    #                 detected_corner = ((line["x1"] + lines[0]["x2"]) // 2, (line["y1"] + lines[0]["y2"]) // 2, lines.index(line), "different")
    #                 break
    #             elif (line["x2"] - lines[0]["x2"]) ** 2 + (line["y2"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
    #                 detected_corner = ((line["x2"] + lines[0]["x2"]) // 2, (line["y2"] + lines[0]["y2"]) // 2, lines.index(line), "same")
    #                 break
    #             elif (line["x1"] - lines[0]["x1"]) ** 2 + (line["y1"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
    #                 detected_corner = ((line["x1"] + lines[0]["x1"]) // 2, (line["y1"] + lines[0]["y1"]) // 2, lines.index(line), "same")
    #                 break
    #         if detected_corner != None:
    #             # if not headless:
    #             # Determine the correct x position for the detected corner based on round_dir
    #             if sm.round_dir == -1:
    #                 corner_x = detected_corner[0]
    #             else:
    #                 corner_x = 640 + detected_corner[0]
    #             cv2.circle(viz, (corner_x, detected_corner[1]), 5, (255, 255 if detected_corner[3] == "different" else 100, 0), -1)
    #             cv2.putText(viz, f"{detected_corner[0]} {detected_corner[1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    #         # calculate the error based on the slope and intercept
    #         if "INNER" in side:
    #             # if we detected a corner, we should start gyro following
    #             if detected_corner != None:
    #                 slope_2 = lines[detected_corner[2]]["m"]
    #                 if (abs(slope) > 3 or abs(slope_2) > 3) and detected_corner[1] > 100 and detected_corner[3] == "same":
    #                     # if we reach the end of the wall
    #                     sm.following_angle = True
    #                     cv2.imwrite(f"logs/image_corner_{12-sm.turns_left}.jpg", viz)
    #                     error = (sm.diff_angle / 80) ** 2
    #             else:
    #                 error = (160 - intercept) / 250 * sm.round_dir
    #                 # increase the bounded error quadratically if the angle is too high
    #                 if sm.diff_angle != 0:
    #                     error = bound(error) - (sm.diff_angle / 80) ** 2 * sm.diff_angle / abs(sm.diff_angle)
    #         elif "OUTER" in side:
    #             error = (intercept - 150) / 250 * sm.round_dir
    #             if sm.diff_angle != 0:
    #                 error = bound(error) - (sm.diff_angle / 80) ** 2 * sm.diff_angle / abs(sm.diff_angle)
    #         elif "MIDDLE" in side: # todo: watch out of blue lines triggering black contour
    #             error = (intercept - 53) / 100 * sm.round_dir
    #     else: # todo remove
    #         print("ERROR: Following the wall without lines")
    #         # if there are no lines in the image, we should follow the wall
    #         if "R" in side:
    #             error = REF_PORTION_SIDE - portion_black_r
    #         else:
    #             error = portion_black_l - REF_PORTION_SIDE

    # if sm.current_state in ["GYRO", "REVERSE-EXTRA"]:
    #     error = -sm.diff_angle / 80
    #     if sm.current_state == "REVERSE-EXTRA":
    #         error *= -1

    # correction = error * kp + (error - last_error) * kd
    
    # if sm.current_state == "TURNING-L":
    #     correction = -turn_correction
    # if sm.current_state == "TURNING-R":
    #     correction = turn_correction

    # driving_speed = speed
    
    # if sm.current_state == "TURNING-REVERSE-L":
    #     correction = turn_correction
    #     driving_speed = -speed
    # if sm.current_state == "TURNING-REVERSE-R":
    #     correction = -turn_correction
    #     driving_speed = -speed

    # if sm.current_state == "REVERSE-EXTRA":
    #     driving_speed = -speed

    # if (("TURN-L-" in sm.current_state and int(sm.current_state[-1]) % 2 == 0) or
    #     ("TURN-R-" in sm.current_state and int(sm.current_state[-1]) % 2 == 1)):
    #     correction = 1
    # elif (("TURN-L-" in sm.current_state and int(sm.current_state[-1]) % 2 == 1) or
    #     ("TURN-R-" in sm.current_state and int(sm.current_state[-1]) % 2 == 0)):
    #     correction = -1
    
    # if sm.current_state == "DONE":
    #     correction = 0.0
    #     print("---- DONE ----")
    #     if ser:
    #         message = "p\n"
    #         ser.write(message.encode())
    #         message = "s0\n"
    #         ser.write(message.encode())
    #     if pillars:
            
    #         sleep(15)
            
    #         ser.write("d-150\n".encode())  # drive backwards for 2 seconds 
    #         ser.write(f"s{int(sm.round_dir * 20)}\n".encode())     # turn in the right direction
    #         sleep(2)                      
    #         ser.write("d0\n".encode())    # Stoppen
    #         # Stoppe Motoren
    #         ser.write("p\n".encode())
    #         ser.write("s0\n".encode())
        
    #     sleep(5)
    #     exit()

    # if sm.search_for_dir and sm.current_state == "STARTING":
    #     sm.round_dir += find_round_dir(rgbl["black"], sm.isPillarRound)
    #     driving_speed = 0
        
    # SPEED_UNPARK = 100
    # if "UNPARKING" in sm.current_state:
    #     if sm.current_state == "UNPARKING-1":
    #         correction = 1.2 if sm.round_dir == -1 else -1.2
    #         driving_speed = SPEED_UNPARK
    #     elif sm.current_state == "UNPARKING-2":
    #         correction = 1.2 if sm.round_dir == 1 else -1.2
    #         driving_speed = -SPEED_UNPARK
    #     elif sm.current_state == "UNPARKING-3":
    #         driving_speed = -SPEED_UNPARK
    #     elif sm.current_state == "UNPARKING-4":
    #         correction = 1 if sm.round_dir == -1 else -1
    #         driving_speed = -SPEED_UNPARK

    # if "AVOID" in sm.current_state and "-3" in sm.current_state:
    #   driving_speed = SPEED_UNPARK # can probably be removed again later on

    # correction = bound(correction)
    # MAX_STEERING_ANGLE = 25.0
    # steering_angle = correction * MAX_STEERING_ANGLE

    # if ser and not calibrate:
    #     message = "d" + str(driving_speed) + "\n"
    #     ser.write(message.encode())
    #     message = "s " + str(int(steering_angle)) + "\n"
    #     ser.write(message.encode())
    correction = 0
    error = 0
    # viz stuff
    if not headless:
        cv2.rectangle(viz, (roi_center_x, roi_center_y), (roi_center_x + roi_center_w, roi_center_y + roi_center_h), (0, 255, 0), 2)
        cv2.rectangle(viz, (0, 0), (roi_width, 150), (255, 0, 0), 2)
        cv2.rectangle(viz, (640-roi_width, 0), (640, 150), (255, 0, 0), 2)
        # cv2.putText(viz, f"State: {sm.current_state} {round(sm.diff_distance)} mm {round(sm.diff_angle, 1)} °", (10, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1) # change to current function that is running
        # cv2.putText(viz, f"direction: {sm.round_dir}, gyro: {sm.following_angle}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz, f"Correction: {round(correction, 2)}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        # cv2.putText(viz, f"{12 - sm.turns_left} / 12", (580, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        if pillars:
            for p in detected_pillars:
                cv2.line(viz, (p.screen_x, 0), (p.screen_x, 480), (0, 0, 255) if p.color == "RED" else (0, 255, 0), 2)    

    last_error = error
    latest_streams = {
        "viz": viz,
        "roi_left": roi_left,
        "roi_right": roi_right,
        "roi_center": roi_center,
        "red": rgbl["red"],
        "green": rgbl["green"],
        "black": rgbl["black"],
        "orange": rgbl["orange"],
        "blue": rgbl["blue"],
        "hsv_image": hsv_image,
        "color_image": color_image,
        # "lines": pipeline.filter_OB(hsv_image)['orange'],
        # "parking": pipeline.filter_parking(hsv_image)
    }
    return latest_streams


async def cycle_loop():
    """
    Main loop that continuously reads data from the Arduino, processes images, and applies control logic.

    This function captures images from the camera, processes them to extract relevant features,
    and applies a PD control algorithm to drive the robot. It also handles state transitions
    based on the robot's current state and sensor readings.

    Returns:
        None
    """
    global last_error, has_sent_streams_info
    last_error = 0.0
    has_sent_streams_info = False

    while True:
        cycle()
        await asyncio.sleep(0.05)  # Sleep for a short duration to prevent blocking

async def img_stream(websocket, path):
    global current_streams, has_sent_streams_info, latest_streams, active_websocket
    print("Websocket connection established")
    # If there is already an active websocket, close it
    if active_websocket is not None and not active_websocket.closed:
        print("Closing previous websocket connection...")
        await active_websocket.close()
    active_websocket = websocket
    # Send initial stream info
    if not has_sent_streams_info:
        has_sent_streams_info = True
        await websocket.send(json.dumps({
            "streams": list(latest_streams.keys()),
        }))

    try:
        while True:
            # check if the websocket has sent a stream request, wait at most for 0.05 seconds
            try:
                res = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.01))
                if "updateGray" in res:
                    # Update the GRAY value in the config
                    config["filters"]["GRAY"] = res["updateGray"]
                    print(f"Updated GRAY value: {config['filters']['GRAY']}")

                    # Save the updated config back to the file
                    with open("config.json", "w") as f:
                        json.dump(config, f, indent=2)
                current_streams[0] = res["streamA"]
                current_streams[1] = res["streamB"]
                current_streams[2] = res["streamC"]
                # print_past_time("received streams")
            except:
                pass

            # Only send if changed
            data = {}
            for idx, stream_name in enumerate(current_streams):
                value = latest_streams.get(stream_name)
                if value is not None:
                    # Use encode_image for images, else send as is
                    if isinstance(value, np.ndarray):
                        encoded = encode_image(value)
                    else:
                        encoded = value
                    data[chr(ord('a') + idx)] = encoded
            await websocket.send(json.dumps(data))
            # print_past_time("sent images")
    finally:
        # If this websocket is the active one, clear it on disconnect
        if active_websocket == websocket:
            active_websocket = None


async def run_webserver():
    async with serve(img_stream, "0.0.0.0", 8765):
        print("Webserver started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

async def main():
    global headless, pillars, shutdown, calibrate, skip_arduino, ser
    parser = argparse.ArgumentParser(description="Check if --headless flag was given.")
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--pillars', action='store_true', help='Run in pillar mode')
    parser.add_argument('--shutdown', action='store_true', help='Shutdown after run')
    parser.add_argument('--calibrate', action='store_true', help='Disable driving and moving to next states')
    parser.add_argument('--skip-arduino', action='store_true', help='Skip Arduino connection')
    args = parser.parse_args()
    headless = args.headless
    pillars = args.pillars
    shutdown = args.shutdown
    calibrate = args.calibrate
    skip_arduino = args.skip_arduino
    
    # setup_logging()
    
    if ser and not calibrate and not skip_arduino:
        connect_arduino(ser) # todo: only wait for start signal in the main_program()

    tasks = [asyncio.create_task(cycle_loop()),
             asyncio.create_task(main_program())]
    if not headless:
        tasks.append(asyncio.create_task(run_webserver()))
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("KeyboardInterrupt received. Stopping robot...")
        try:
            if 'ser' in globals() and ser is not None:
                ser.write(b's0\n')  # Stop steering
                ser.write(b'p\n')   # Stop driving
                print("Sent stop commands to robot via serial.")
        except Exception as e:
            print(f"Error sending stop commands: {e}")
        raise

def encode_image(image):
    retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 99])
    base64_str = base64.b64encode(buffer).decode('utf-8')
    return base64_str

async def drive(speed, distance):
    while (distance > 0):
        await asyncio.sleep(0.1)  # Simulate driving

async def main_program():
    speed = 300 if not pillars else 200
    print("Starting main program...")
    await drive(speed, 1000)  # Example drive command, adjust as needed

if __name__ == "__main__":
    asyncio.run(main())