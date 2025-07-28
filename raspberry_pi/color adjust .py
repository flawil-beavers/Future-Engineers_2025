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
from helpers import Pillar, extract_ROI
from pipeline import Pipeline
from statemachine import StateMachine
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera

configloader = ConfigLoader("config.json")
pipeline = Pipeline(configloader)

# picam2 = Picamera2()
# # picam2.configure(picam2.create_preview_configuration())
# preview_config = picam2.create_preview_configuration()
# preview_config["transform"] = libcamera.Transform(vflip=True, hflip=True)
# picam2.configure(preview_config)
# picam2.start()

# picam2.set_controls({
#     "AwbEnable": False,
#     # controls.AWB_TEMPERATURE: fixed_temperature
# })


# ports = serial.tools.list_ports.comports()


# try:
#   ser = serial.Serial(configloader.get_property("ArduinoSerialPort"), 9600)
# except:
#   print("Arduino not connected, available devices")
#   ser = None
#   for port, desc, hwid in sorted(ports):
#         print("{}: {} [{}]".format(port, desc, hwid))

image_name = "IMG2.jpg"

# load an image from the logs and run the edge detection on it
def load_image_from_logs():
    # Load the image from the logs
    image_path = os.path.join("logs", image_name)
    if not os.path.exists(image_path):
        print(f"Image not found at {image_path}")
        return None

    # Read the image
    image = cv2.imread(image_path)
    if image is None:
        print(f"Failed to read image from {image_path}")
        return None

    return image

def process_image(image, roi=None):
    # Convert the image to RGB
    color_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    hsv_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)
  
    rgbl = pipeline.filter_RG_Bl(hsv_image, color_image)
    
    roi_width = 100
    if roi == "left":
        black_image = extract_ROI(rgbl["black"], [0, 0], [roi_width, 150])
    elif roi == "right":
        black_image = extract_ROI(rgbl["black"], [640-roi_width, 0], [640, 150])
    else :
        black_image = rgbl["black"]
   
    
    # do edge detection on the black image
    blurredImg = cv2.GaussianBlur(black_image, (3, 3), 0)
    lower = 30
    upper = 90
    edges_img = cv2.Canny(blurredImg, lower, upper, 3)
    # make the bottom row all white, so we don't detect the floor
    # edges_img = cv2.line(edges_img, (0, 0), (edges_img.shape[1], 0), 1, 1)
    # edges_img = cv2.line(edges_img, (0, edges_img.shape[0]-1), (edges_img.shape[1], edges_img.shape[0]-1), 1, 1)
    
    # find long consistent lines and print their equations next to the lines
    # find the lines in the image
    lines = cv2.HoughLinesP(edges_img, 1, np.pi/180, 2, minLineLength=50, maxLineGap=50)
    if lines is not None:
        for line in lines:
            x1, y1, x2, y2 = line[0]
            # if y1 == y2:
            #     continue
            # draw each line with a different color
            color = (int(x1), 255, int(y1))
            cv2.line(color_image, (x1, y1), (x2, y2), color, 2)
            # calculate the slope and intercept of the line
            slope = (y2 - y1) / (x2 - x1)
            intercept = y1 - slope * x1
            # print the equation of the line
            print(f"y = {slope:.2f}x + {intercept:.2f}")
    else:
        print("No lines found")  
  
    # save the image to the same folder with the ending _edges
    edges_image_path = os.path.join("logs", image_name.replace(".jpg", "_edges.jpg"))
    cv2.imwrite(edges_image_path, color_image)

if __name__ == "__main__":
    # Load the image from the logs
    image = load_image_from_logs()
    if image is None:
        print("No image to process")
        exit(1)

    # Process the image
    process_image(image, roi="left")
    print(f"Processed image saved to logs/{image_name.replace('.jpg', '_edges.jpg')}")