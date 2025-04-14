#include <Arduino.h>
#include <Servo.h>
#include <PinChangeInterrupt.h>
// add function to use the adafruit lsm303dlhc and l3gd20 sensors with sensor fusion
#include <Adafruit_Sensor.h>
#include <Adafruit_LSM303_Accel.h>
#include <Adafruit_LSM303DLH_Mag.h>
#include <Adafruit_L3GD20_U.h>
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

unsigned long current_time = 0;
unsigned long last_time = 0;
unsigned long last_status_time = 0;  // when the last status was printed
unsigned long last_loop_time_us = 0; // last loop time in microseconds
float last_loop_time = 0;            // last loop time in seconds

sensors_event_t event;
float degree = 0;
float degree_calibrated = 0;
float offset = 0;
float last_offset = 0;
// float y = -0.0006x - 0.0034
float offset_m = -0.0006;
float offset_b = -0.0034;
float degree_calibrated_temp = 0;

unsigned long last_offset_time = 0;
const float scaling_calibrated = 1800 / 1750.03; // deg measured for 5 rotations

int temperature = 0;
float temperature_average = 0; // average temperature since last offset time

// initialise gyro
Adafruit_L3GD20_Unified gyro;

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

int get_temperature()
{
  Wire.beginTransmission(L3GD20_ADDRESS);
  Wire.write(GYRO_REGISTER_OUT_TEMP); // Set read bit
  Wire.endTransmission(false);        // Send repeated start
  Wire.requestFrom(L3GD20_ADDRESS, 1);

  if (Wire.available())
  {
    temperature = (int8_t)Wire.read(); // Read as signed 8-bit integer
  }
  return temperature;
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

void update_gyro()
{
  gyro.getEvent(&event);
  degree += event.gyro.z * last_loop_time;
  degree_calibrated_temp += (event.gyro.z - (offset_m * get_temperature() + offset_b) / 2) * last_loop_time * scaling_calibrated; // / 2 experimentally included
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

  delay(30);

  if (!gyro.begin())
  {
    /* There was a problem detecting the L3GD20 ... check your connections */
    Serial.println("Ooops, no L3GD20 detected ... Check your wiring!");
    while (1)
      ;
  }
  gyro.enableAutoRange(true);
}

int a = 0;

void loop()
{
  current_time = micros();
  last_loop_time_us = current_time - last_time;
  last_loop_time = last_loop_time_us / 1000000.0; // in seconds

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
  // drive(en_state ? _set_speed : 0);
  checkEnable();
  update_gyro();
  // set_dc(dc_to_set_temp);
  if (current_time - last_status_time > 200000)
  {
    last_status_time = current_time;
    Serial.print("time passed (ms): ");
    Serial.print(last_loop_time * 1000);
    Serial.print(" temperature: ");
    Serial.print(get_temperature());
    Serial.print(" degree: ");
    Serial.print(degree * 180 / PI);
    Serial.print(" degree_calibrated_temp: ");
    Serial.print(degree_calibrated_temp * 180 / PI);
    Serial.print("\r\n");
  }
  if (current_time - last_offset_time > 100000000)
  {
    last_offset_time = current_time;
    offset = degree - last_offset;
    // Serial.print("offset: ");
    Serial.print(offset / 100, 6);
    // Serial.print(" temperature: ");
    Serial.print(", ");
    Serial.println(temperature_average / 100, 6);
    temperature_average = 0;
    last_offset += offset;
    // delay(100000);
  }
  last_time = current_time;
}
