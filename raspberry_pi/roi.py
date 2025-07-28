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
from helpers import Pillar, extract_ROI, print_past_time, Straight_Section, Lines, bound
from pipeline import Pipeline
from statemachine import StateMachine
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera
import sys

#?: Webviewer controls for tuning colors, pd, and selecting stream

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

def pause_robot():
  print("Robot paused")
  while ser and not ser.readline().decode('utf-8').strip() == "enable 1":
    sleep(0.1)
  print("Robot resumed")  

def cycle():
  global sm, last_error, kp, kd, straight_sections, angle_following
  distance = 0.0
  angle = 0.0
  
  # Send n and g to the Arduino to get the distance and gyro heading
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
  sm.update_distance(distance) # takes all together 20-35 ms
  sm.update_angle(angle)
  # print_past_time(f"gotten distance {distance} and gyro {gyro}")

  # image reading, usually form camera
  img = cv2.cvtColor(picam2.capture_array(), cv2.COLOR_RGB2BGR) # 5-10ms
  # print_past_time("gotten image")
  
  # undistorted = pipeline.undistort(img)
  color_image = pipeline.crop(img)

  # copy for webviewer visualization
  viz = color_image.copy()

  # convert to hsv, for color filtering
  hsv_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)

  # center region-of-interest for detecting the turn marker lines
  roi_center_w, roi_center_h = 100, 50
  roi_center_x, roi_center_y = 320 - roi_center_w // 2, 180
  roi_center = extract_ROI(hsv_image, [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
  
  # filter out the orange and blue colors of turn markers
  rgbl = pipeline.filter_RG_Bl(hsv_image, color_image)

  # l and r ROIs used for PD control, to keep the car in the middle of the track and away from walls
  roi_width = 100
  roi_height = 150
  roi_left_side = extract_ROI(rgbl["black"], [0, 0], [roi_width, rgbl["black"].shape[0]])
  roi_right_side = extract_ROI(rgbl["black"], [640-roi_width, 0], [640, rgbl["black"].shape[0]])
  
  roi_left = extract_ROI(roi_left_side, [0, 0], [roi_width, roi_height])
  roi_right = extract_ROI(roi_right_side, [0, 0], [roi_width, roi_height])

  portion_black_l = cv2.countNonZero(roi_left) / (roi_left.shape[0] * roi_left.shape[1])
  portion_black_r = cv2.countNonZero(roi_right) / (roi_right.shape[0] * roi_right.shape[1])

  if pillars:
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

    for line_group in border_lines.values():
      if line_group is not None:
        b = 200
        for line in line_group.lines:
          if not headless:
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

    roi_front_width = 10
    roi_front = extract_ROI(rgbl["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
    portion_black_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])
    sm.distance_front = portion_black_front
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

    section_index = (11-sm.turns_left) % 4
    if sm.take_picture and sm.distance_take_picture < distance:
      sm.take_picture = False
      sm._took_picture = True
      index = None
      if straight_sections[section_index].parking_lot:
        print(f"parking lot in section, resetting pillars")
        # rescan the parking lot
        for i in range(3):
          straight_sections[section_index].l[i] = 0
          straight_sections[section_index].r[i] = 0
        print(f"pillars reset and printing now")
        straight_sections[section_index].print()
      for p in detected_pillars:
        if p.ignore:
          continue
        if sm.current_state == "PD-CENTER-START":
          if p.y > 130:
            print(f"--Pillar {p.color} is too high, y={p.y}")
            continue
          if abs(p.screen_x - 320) > 170:
            print(f"--Pillar {p.color} is too far from the center, x={p.screen_x}")
            continue
          if p.y > 50:
            index = 0
          elif p.y > 28:
            index = 1
          # elif p.y > 24:
          #   index = 2
          else:
            print(f"--Pillar {p.color} is too low, y={p.y}")
            continue
        else:
          if p.y > 180:
            print(f"--Pillar {p.color} is too high, y={p.y}")
            continue
          if p.y > 50:
            index = 0
          elif p.y > 35:
            index = 1
          elif p.y > 24:
            index = 2
          else:
            print(f"--Pillar {p.color} is too low, y={p.y}")
            continue
        if p.screen_x < 320:
          straight_sections[section_index].l[index] = p.color
        else:
          straight_sections[section_index].r[index] = p.color
        cv2.rectangle(viz, (p.screen_x - int(p.width*0.35), p.y-p.height), (p.screen_x + int(p.width*0.35), p.y), ((0, 0, 255) if p.color == "RED" else (0, 255, 0)), 3)
        cv2.putText(viz, f"{p.color} {int(p.y)} {index}", (p.screen_x - int(p.width*0.35), p.y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

      straight_sections[section_index].parking_lot = True if section_index == 3 else False
      straight_sections[section_index].validate(sm.round_dir)
      cv2.imwrite(f"logs/image{section_index}{'_p' if sm.current_state == 'PD-CENTER-START' else ''}.jpg", color_image)
      cv2.imwrite(f"logs/image_viz{section_index}{'_p' if sm.current_state == 'PD-CENTER-START' else ''}.jpg", viz)
      print("Image saved")
      sm.pillar_driving_pos = straight_sections[section_index].calculate_driving_pos() # todo: in last example red was not saved although it was detected
      straight_sections[section_index].print()

    if sm.current_state == "PD-CENTER-2" and not sm._took_picture:
      if sm.distance_take_picture < sm.total_distance:
        sm._took_picture = True
        sm.pillar_driving_pos = straight_sections[section_index].calculate_driving_pos()
        print(f"Picture would be taken now")

  # A state machine is used to model the car's behavior
  # This checks if the car should transition to a new state, and if so, transitions
  # states may be PD-CENTER, PD-RIGHT, PD-LEFT, TURNING-L, TURNING-R, etc.
  if not calibrate:
    if pillars:
      sm.shouldTransitionState()
    else:
      orange_roi = extract_ROI(rgbl["orange"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
      blue_roi = extract_ROI(rgbl["blue"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
      portion_orange = cv2.countNonZero(orange_roi) / (orange_roi.shape[0] * orange_roi.shape[1])
      portion_blue = cv2.countNonZero(blue_roi) / (blue_roi.shape[0] * blue_roi.shape[1])
      sm.shouldTransitionState(portion_orange, portion_blue)

  # PD control

  # This is the reference value for the single side PD control, 
  # eg. how much black should be on the left side when the car follows the left outer wall

  # pillar_ref = 0.35
  # if sm.next_pillar:
  #   if sm.next_pillar.ignore:
  #     pillar_ref = 0.48
    

  REF_PORTION = 0.45 if not sm.isPillarRound else 0.30
  REF_PORTION_SIDE = 0.8

  if "PD-CENTER-" in sm.current_state:
    REF_PORTION = 0.4
  
  # error value
  error = 0.0

  turn_correction = 0.75 if not sm.isPillarRound else 1.0

  PD_STATES = ["PD-CENTER", "PD-CENTER-START", "PD-CENTER-PARKING-1", "PD-CENTER-PARKING-2"]

  # follow the left wall, if we're going counter-clockwise
  if sm.current_state in PD_STATES and sm.round_dir == -1:
    error = (REF_PORTION - portion_black_r) * 1.2

  # follow the right wall, if we're going clockwise
  if sm.current_state in PD_STATES and sm.round_dir == 1:
    error = (portion_black_l - REF_PORTION) * 1.2
  
  if pillars and sm.side != None:
    side = sm.side
    if sm.following_angle == True:
      error = -sm.diff_angle / 80
    elif sm.round_dir == -1 and portion_black_r > 0.99 and "INNER" in side:
      # if the right side is black, we should follow the left wall
      error = REF_PORTION - portion_black_r
    elif sm.round_dir == 1 and portion_black_l > 0.99 and "INNER" in side:
      # if the left side is black, we should follow the right wall
      error = portion_black_l - REF_PORTION
    elif len(border_lines[side[-1]].lines) > 0:
      # if there are lines in the image, we should follow them      
      lines = border_lines[side[-1]].lines
      # calculate the slope and intercept of the line
      slope = lines[0]["m"]
      intercept = lines[0]["b"]
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
        if sm.round_dir == -1:
            corner_x = detected_corner[0]
        else:
            corner_x = 640 + detected_corner[0]
        cv2.circle(viz, (corner_x, detected_corner[1]), 5, (255, 255 if detected_corner[3] == "different" else 100, 0), -1)
        cv2.putText(viz, f"{detected_corner[0]} {detected_corner[1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
      # calculate the error based on the slope and intercept
      if "INNER" in side:
          # if we detected a corner, we should start gyro following
        if detected_corner != None:
          slope_2 = lines[detected_corner[2]]["m"]
          if (abs(slope) > 3 or abs(slope_2) > 3) and detected_corner[1] > 100 and detected_corner[3] == "same":
            # if we reach the end of the wall
            sm.following_angle = True
            cv2.imwrite(f"logs/image_corner_{12-sm.turns_left}.jpg", viz)
            error = (sm.diff_angle / 80) ** 2
        else:
          error = (160 - intercept) / 250 * sm.round_dir
          # increase the bounded error quadratically if the angle is too high
          if sm.diff_angle != 0:
            error = bound(error) - (sm.diff_angle / 80) ** 2 * sm.diff_angle / abs(sm.diff_angle)
      elif "OUTER" in side:
        error = (intercept - 150) / 250 * sm.round_dir
        if sm.diff_angle != 0:
          error = bound(error) - (sm.diff_angle / 80) ** 2 * sm.diff_angle / abs(sm.diff_angle)
      elif "MIDDLE" in side: # todo: watch out of blue lines triggering black contour
        error = (intercept - 53) / 100 * sm.round_dir
    else: # todo remove
      print("ERROR: Following the wall without lines")
      # if there are no lines in the image, we should follow the wall
      if "R" in side:
        error = REF_PORTION_SIDE - portion_black_r
      else:
        error = portion_black_l - REF_PORTION_SIDE

  if sm.current_state in ["GYRO", "REVERSE-EXTRA"]:
    error = -sm.diff_angle / 80
    if sm.current_state == "REVERSE-EXTRA":
      error *= -1

  correction = error * kp + (error - last_error) * kd
  
  if sm.current_state == "TURNING-L":
    correction = -turn_correction
  if sm.current_state == "TURNING-R":
    correction = turn_correction

  driving_speed = speed
  
  if sm.current_state == "TURNING-REVERSE-L":
    correction = turn_correction
    driving_speed = -speed
  if sm.current_state == "TURNING-REVERSE-R":
    correction = -turn_correction
    driving_speed = -speed

  if sm.current_state == "REVERSE-EXTRA":
    driving_speed = -speed

  if (("TURN-L-" in sm.current_state and int(sm.current_state[-1]) % 2 == 0) or
      ("TURN-R-" in sm.current_state and int(sm.current_state[-1]) % 2 == 1)):
    correction = 1
  elif (("TURN-L-" in sm.current_state and int(sm.current_state[-1]) % 2 == 1) or
        ("TURN-R-" in sm.current_state and int(sm.current_state[-1]) % 2 == 0)):
    correction = -1
  
  if sm.current_state == "DONE":
    correction = 0.0
    print("---- DONE ----")
    if ser:
      message = "p\n"
      ser.write(message.encode())
      message = "s0\n"
      ser.write(message.encode())
    # exit()
    sleep(5)
    exit()

  if sm.search_for_dir and sm.current_state == "STARTING":
    sm.round_dir += find_round_dir(rgbl["black"], sm.isPillarRound)
    driving_speed = 0
    
  SPEED_UNPARK = 100
  if "UNPARKING" in sm.current_state:
    if sm.current_state == "UNPARKING-1":
      correction = 1.2 if sm.round_dir == -1 else -1.2
      driving_speed = SPEED_UNPARK
    elif sm.current_state == "UNPARKING-2":
      correction = 1.2 if sm.round_dir == 1 else -1.2
      driving_speed = -SPEED_UNPARK
    elif sm.current_state == "UNPARKING-3":
      driving_speed = -SPEED_UNPARK
    elif sm.current_state == "UNPARKING-4":
      correction = 1 if sm.round_dir == -1 else -1
      driving_speed = -SPEED_UNPARK

  # if "AVOID" in sm.current_state and "-3" in sm.current_state:
  #   driving_speed = SPEED_UNPARK # can probably be removed again later on

  correction = bound(correction)
  MAX_STEERING_ANGLE = 25.0
  steering_angle = correction * MAX_STEERING_ANGLE


  if ser and not calibrate:
    message = "d" + str(driving_speed) + "\n"
    ser.write(message.encode())
    message = "s " + str(int(steering_angle)) + "\n"
    ser.write(message.encode())
  
  # viz stuff
  if not headless:
    cv2.rectangle(viz, (roi_center_x, roi_center_y), (roi_center_x + roi_center_w, roi_center_y + roi_center_h), (0, 255, 0), 2)
    cv2.rectangle(viz, (0, 0), (roi_width, 150), (255, 0, 0), 2)
    cv2.rectangle(viz, (640-roi_width, 0), (640, 150), (255, 0, 0), 2)
    cv2.putText(viz, f"State: {sm.current_state} {round(sm.diff_distance)} mm {round(sm.diff_angle, 1)} °", (10, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    cv2.putText(viz, f"direction: {sm.round_dir}, gyro: {sm.following_angle}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    cv2.putText(viz, f"Correction: {round(correction, 2)}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    cv2.putText(viz, f"{12 - sm.turns_left} / 12", (580, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    if pillars:
      for p in detected_pillars:
        cv2.line(viz, (p.screen_x, 0), (p.screen_x, 480), (0, 0, 255) if p.color == "RED" else (0, 255, 0), 2)    

  last_error = error
  # print_past_time("finished cycle") # 50 ms
  return {
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


def main():
  global sm, last_error, kp, kd, straight_sections
  sm = StateMachine(isPillarRound=pillars)
  # sm.transitionState("AVOID-L")
  last_error = 0.0

  kp = configloader.get_property("PD")['kp']
  kd = configloader.get_property("PD")['kd']
  
  # create a list with four elements of Straight_Section
  straight_sections = [Straight_Section(i) for i in range(4)]

  try:
    while True:
      cycle()
  except (KeyboardInterrupt):
    if ser:
      ser.write("s0\n".encode())
      ser.write("p\n".encode())
    exit()

def encode_image(image):
  retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 99])
  base64_str = base64.b64encode(buffer).decode('utf-8')
  return base64_str

# Load the config file
with open("config.json", "r") as f:
  config = json.load(f)

async def img_stream(websocket: WebSocketServerProtocol, path):
  global sm, last_error, kp, kd, straight_sections
  sm = StateMachine(isPillarRound=pillars)
  # sm.round_dir = 1
  # sm.transitionState("AVOID-R-1")
  last_error = 0.0

  # create a list with four elements of Straight_Section
  straight_sections = [Straight_Section(i) for i in range(4)]

  kp = configloader.get_property("PD")['kp']
  kd = configloader.get_property("PD")['kd']

  has_sent_streams_info = False
  current_streams = ["viz", "black", "color_image"]
  try:
    while True:
      products = cycle() 
      if not has_sent_streams_info:
        has_sent_streams_info = True
        await websocket.send(json.dumps({
          "streams": list(products.keys())
        }))
      
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

      data = {
        "a": encode_image(products[current_streams[0]]),
        "b": encode_image(products[current_streams[1]]),
        "c": encode_image(products[current_streams[2]])
      } # takes approximately 20 ms - 40 ms
      # print_past_time("encoded images")
      await websocket.send(json.dumps(data)) # takes up to 80ms
      # print_past_time("sent images")
  except (KeyboardInterrupt):
    if ser:
      ser.write("s0\n".encode())
      ser.write("p\n".encode())
    exit()
    

if __name__ == "__main__":
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
  
  # Open a file for logging
  log_file = open("logs/robot_log.txt", "a")

  # Redirect print statements to the log file
  class Logger:
    def __init__(self, file):
      self.file = file

    def write(self, message):
      self.file.write(message)
      self.file.flush()

    def flush(self):
      pass

  sys.stdout = Logger(log_file)
  sys.stderr = Logger(log_file)
  
  speed = 300 if not pillars else 200
  
  if ser and not calibrate and not skip_arduino:
    print("Connecting to Arduino")
    while True:
      ser.timeout = 2
      msg = ser.readline().decode('utf-8')
      msg = msg.strip()
      if msg == "Gyro OK":
        break
      else:
        if msg == "Gyro error":
          print("Gyro error, closing serial and restarting connection")
        else:
          print("Arduino not available, closing serial and restarting connection")
        ser.setDTR(False)
        sleep(1)
        ser.setDTR(True)
        continue
      
    ser.timeout = None
    print("Gyro initialized successfully")
    ser.write("o\n".encode())
    while True:
      msg = ser.readline().decode('utf-8')
      msg = msg.strip()
      if msg == "enable 1":
        break
    print("Arduino connected")
  if headless:
    main()
  else:
    start_server = serve(img_stream, "0.0.0.0", 8765)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()
    if shutdown:
      os.system("sudo shutdown now")
