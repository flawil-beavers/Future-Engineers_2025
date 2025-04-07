#include <Arduino.h>
#include <Servo.h>
#include <PinChangeInterrupt.h>
// add function to use the adafruit lsm303dlhc and l3gd20 sensors with sensor fusion
#include <Adafruit_Sensor.h>
#include <Adafruit_LSM303_Accel.h>
#include <Adafruit_LSM303DLH_Mag.h>
#include <L3G.h>
#include <Wire.h>

// const int sdaPin = 18;
// const int sclPin = 19;

const int servoPin = 2;

Servo servo;

const int enaPin = 11;
const int in1Pin = 5;
const int in2Pin = 6;

const int enTogglePin = 7;

const int encoderPinA = 3;
const int encoderPinB = 4;

const int countperrev = 1807;

int encoder_pos = 0;
int encoder_dir = 1; // 1 -> CCW, -1 -> CW

bool en_state = false;

int current_speed = 0;
int set_speed = 0;
unsigned long acc_time = 20;
unsigned long last_acc_time = 0;

int middle = 97; // +55 -55
int degree_max = middle + 30;
int degree_min = middle - 30;
int current_degree = 0;
int set_degree = 0;

// initialise gyro
L3G gyro;

// initialise accel
Adafruit_LSM303_Accel_Unified accel = Adafruit_LSM303_Accel_Unified(30301);

// initialise magnetometer
Adafruit_LSM303DLH_Mag_Unified mag = Adafruit_LSM303DLH_Mag_Unified(12345);

#define BUFFER_SIZE 64

char ringBuffer[BUFFER_SIZE];
int head = 0;
int tail = 0;

void drive(int speed)
{
  if (speed > 200)
  {
    speed = 200;
  }
  else if (speed < -200)
  {
    speed = -200;
  }
  if (millis() < last_acc_time + acc_time)
  {
    return;
  }
  last_acc_time = millis();
  if (abs(speed - current_speed) > 1)
  {
    current_speed = current_speed + (speed - current_speed) / fabs(speed - current_speed) * 1;
  }
  else if (speed == 0)
  {
    current_speed = 0;
  }
  if (current_speed > 0)
  {
    digitalWrite(in1Pin, LOW);
    digitalWrite(in2Pin, HIGH);
  }
  else if (current_speed < 0)
  {
    digitalWrite(in1Pin, HIGH);
    digitalWrite(in2Pin, LOW);
  }
  else
  {
    digitalWrite(in1Pin, LOW);
    digitalWrite(in2Pin, LOW);
  }
  analogWrite(enaPin, abs(current_speed));
}

void steer(int angle)
{
  angle = angle + middle;
  if (angle > degree_max)
  {
    angle = degree_max;
  }
  else if (angle < degree_min)
  {
    angle = degree_min;
  }
  servo.write(angle);
}

void parseMessage(char *msg)
{
  char cmd[3]; // To store the 2-char command
  int value = 0;

  sscanf(msg, "%1s", cmd);

  // skip whitespace
  char *beg = ++msg;

  while (*beg == ' ')
  {
    beg++;
  }

  char *end = beg;

  while (*end != '\0')
  {
    end++;
  }

  // Serial.println(cmd);
  value = atoi(beg);
  // Serial.println(value);

  if (cmd[0] == 'd')
  {
    set_speed = value;
  }
  else if (cmd[0] == 's')
  {
    set_degree = value;
  }
  else if (cmd[0] == 'e')
  {
    Serial.println(encoder_pos);
  }
}

void processMessage()
{
  // Message extraction from ring buffer
  char message[BUFFER_SIZE];
  int index = 0;
  while (tail != head)
  {
    char currentChar = ringBuffer[tail];
    tail = (tail + 1) % BUFFER_SIZE;

    if (currentChar == '\n')
    { // End of message
      break;
    }

    message[index++] = currentChar;
  }

  message[index] = '\0'; // Null-terminate the message string
  // Serial.println(message);

  // Parse the extracted message
  parseMessage(message);
}

void checkEnable()
{
  bool last_state = en_state;
  en_state = digitalRead(enTogglePin) == HIGH;

  if (en_state != last_state)
  {
    if (en_state)
    {
      Serial.println("enable 1");
    }
    else
    {
      Serial.println("enable 0");
    }
    delay(250);
  }
}

void update_encoder(int encoderPin)
{
  int a = digitalRead(encoderPinA);
  int b = digitalRead(encoderPinB);
  if ((a == b && encoderPin == encoderPinA) || (a != b && encoderPin == encoderPinB))
  {
    encoder_dir = 1;
  }
  else
  {
    encoder_dir = -1;
  }
  encoder_pos += encoder_dir;
}

void update_encoder_a()
{
  update_encoder(encoderPinA);
}

void update_encoder_b()
{
  update_encoder(encoderPinB);
}

void setup()
{
  pinMode(in1Pin, OUTPUT);
  pinMode(in2Pin, OUTPUT);

  pinMode(enTogglePin, INPUT);

  servo.attach(servoPin);

  pinMode(encoderPinA, INPUT);
  pinMode(encoderPinB, INPUT);

  digitalWrite(in1Pin, LOW);
  digitalWrite(in2Pin, LOW);
  analogWrite(enaPin, 0);
  Serial.begin(115200);

  attachPinChangeInterrupt(digitalPinToPinChangeInterrupt(encoderPinA), update_encoder_a, CHANGE);
  attachPinChangeInterrupt(digitalPinToPinChangeInterrupt(encoderPinB), update_encoder_b, CHANGE);

  if (!gyro.init())
  {
    /* There was a problem detecting the L3GD20 ... check your connections */
    Serial.println("Ooops, no L3GD20 detected ... Check your wiring!");
    while (1)
      ;
  }

  sensor_t sensor;

  if (!mag.begin())
  {
    /* There was a problem detecting the LSM303 ... check your connections */
    Serial.println("Ooops, no LSM303 detected ... Check your wiring!");
    while (1)
      ;
  }

  if (!accel.begin())
  {
    /* There was a problem detecting the ADXL345 ... check your connections */
    Serial.println("Ooops, no LSM303 detected ... Check your wiring!");
    while (1)
      ;
  }

  // sensor_t sensor;
  accel.getSensor(&sensor);
  Serial.println("------------------------------------");
  Serial.print("Sensor:       ");
  Serial.println(sensor.name);
  Serial.print("Driver Ver:   ");
  Serial.println(sensor.version);
  Serial.print("Unique ID:    ");
  Serial.println(sensor.sensor_id);
  Serial.print("Max Value:    ");
  Serial.print(sensor.max_value);
  Serial.println(" m/s^2");
  Serial.print("Min Value:    ");
  Serial.print(sensor.min_value);
  Serial.println(" m/s^2");
  Serial.print("Resolution:   ");
  Serial.print(sensor.resolution);
  Serial.println(" m/s^2");
  Serial.println("------------------------------------");
  Serial.println("");
  delay(500);

  accel.setRange(LSM303_RANGE_4G);
  Serial.print("Range set to: ");
  lsm303_accel_range_t new_range = accel.getRange();
  switch (new_range)
  {
  case LSM303_RANGE_2G:
    Serial.println("+- 2G");
    break;
  case LSM303_RANGE_4G:
    Serial.println("+- 4G");
    break;
  case LSM303_RANGE_8G:
    Serial.println("+- 8G");
    break;
  case LSM303_RANGE_16G:
    Serial.println("+- 16G");
    break;
  }
  accel.setMode(LSM303_MODE_NORMAL);
  Serial.print("Mode set to: ");
  lsm303_accel_mode_t new_mode = accel.getMode();
  switch (new_mode)
  {
  case LSM303_MODE_NORMAL:
    Serial.println("Normal");
    break;
  case LSM303_MODE_LOW_POWER:
    Serial.println("Low Power");
    break;
  case LSM303_MODE_HIGH_RESOLUTION:
    Serial.println("High Resolution");
    break;
  }
}

int a = 0;

void loop()
{
  while (Serial.available() > 0)
  {
    char incomingByte = Serial.read();
    // Serial.print(incomingByte);
    ringBuffer[head] = incomingByte;
    head = ++head % BUFFER_SIZE; // Move the head and wrap it around

    // If head meets tail, it means buffer overflow, so move tail forward
    if (head == tail)
    {
      tail = ++tail % BUFFER_SIZE;
    }

    // Check for the end of the message (newline '\n')
    if (incomingByte == '\n')
    {
      processMessage();
      // Serial.print("encoder_pos: ");
      // Serial.println(encoder_pos);
    }
  }
  steer(en_state ? set_degree : 0);
  drive(en_state ? set_speed : 0);
  checkEnable();
  
  sensors_event_t event;

  /* Display the results (acceleration is measured in m/s^2) */
  Serial.print("X: ");
  Serial.print(event.acceleration.x);
  Serial.print("  ");
  Serial.print("Y: ");
  Serial.print(event.acceleration.y);
  Serial.print("  ");
  Serial.print("Z: ");
  Serial.print(event.acceleration.z);
  Serial.print("  ");
  Serial.println("m/s^2");

  mag.getEvent(&event);

  // Calculate the angle of the vector y,x
  float heading = (atan2(event.magnetic.y, event.magnetic.x) * 180) / PI;

  // Normalize to 0-360
  if (heading < 0)
  {
    heading = 360 + heading;
  }
  Serial.print("Compass Heading: ");
  Serial.println(heading);

  /* Delay before the next sample */
  delay(500);
  // byte error, address;
  // int nDevices = 0;
  // for(address = 1; address < 127; address++ )
  // {
  //   Wire.beginTransmission(address);
  //   error = Wire.endTransmission();
  //   if (error == 0)
  //   {
  //     Serial.print("I2C device found at address 0x");
  //     if (address < 16)
  //     {
  //       Serial.print("0");
  //     }
  //     Serial.print(address, HEX);
  //     Serial.println(" !");

  //     nDevices++;
  //   }
  //   else if (error == 4)
  //   {
  //     Serial.print("Unknown error at address 0x");
  //     if (address < 16)
  //     {
  //       Serial.print("0");
  //     }
  //     Serial.println(address, HEX);
  //   }
  // }
  // if (nDevices == 0)
  // {
  //   Serial.println("No I2C devices found\n");
  // }
  // else
  // {
  //   Serial.println("done\n");
  // }
  gyro.read();
  Serial.print("X: ");
  Serial.print(gyro.g.x);
  Serial.print("  ");
  Serial.print("Y: ");
  Serial.print(gyro.g.y);
  Serial.print("  ");
  Serial.print("Z: ");
  Serial.print(gyro.g.z);
  Serial.print("  ");
  Serial.println("dps\n\r");
}
