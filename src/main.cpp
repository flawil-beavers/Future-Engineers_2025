#include <Arduino.h>
#include <Servo.h>
#include "PinChangeInterrupt.h"

Servo servo;

// pin definitions
const int servoPin = 2;

const int enaPin = 11;
const int in1Pin = 5;
const int in2Pin = 6;

const int enTogglePin = 7;

const int encoderPinA = 3;
const int encoderPinB = 4;

// ------ drive settings ------
// encoder settings
const int countperrev = 1807;
const float counter_to_mm = 20.0 / 28.0 * PI * 62.4 / countperrev; // mm per encoder count

long encoder_pos = 0;
int encoder_dir = 1; // 1 -> CCW, -1 -> CW

bool en_state = false; // enable state

int max_dc = 255;       // max duty cycle for motor driver
int min_dc = 0;         // min duty cycle for motor driver
float max_acc_dc = 255; // max acceleration duty cycle for motor driver (dc/s)
float current_dc = 0;   // current duty cycle for motor driver
float acc = 200;        // acceleration speed (mm/s^2)
bool enable_dc = true;  // enable dc motor
bool hold_dc = false;

// speed settings
float current_speed = 0;
int _set_speed = 0;
int target_speed = 0; // target speed for the motor in mm/s
unsigned long acc_time = 20;
unsigned long last_acc_time = 0;

// steering settings
int middle = 97; // +55 -55
int degree_max = middle + 30;
int degree_min = middle - 30;
int set_degree = 0;

// PID
float target_distance = 0; // target encoder position in mm

int dc_to_set_temp = 0;
float measured_speed = 0;   // measured speed in mm/s
float current_distance = 0; // current distance in mm
float last_distance = 0;    // last distance in mm
float Kp = 0.8;             // proportional gain for PID controller
float Ki = 1.0;             // integral gain for PID controller
float Kd = 0.4;             // derivative gain for PID controller
float i_max = 1.0;          // max integral value for PID controller
float pid_integral = 0.0;   // integral term for PID controller
float last_error = 0.0;     // last error for PID controller

// time variables
unsigned long current_time = 0;
unsigned long last_time = 0;
unsigned long last_status_time = 0; // when the last status was printed
float last_loop_time = 0;           // last loop time in seconds
float last_loop_time_us = 0;        // last loop time in microseconds

#define BUFFER_SIZE 64

char ringBuffer[BUFFER_SIZE];
int head = 0;
int tail = 0;

// debug variables
int dc_out = 0;
float pid_before_checking = 0;

/*
Set the duty cycle of the motor driver.
The duty cycle is limited by max_dc, min_dc and max_acc_dc.
dc can be a positive or negative value.
*/
void set_dc(float dc)
{
  if (!enable_dc)
  {
    return;
  }
  if (dc != 0 && fabs(dc) > max_dc)
  {
    dc = max_dc * (dc / fabs(dc));
  }
  else if (dc != 0 && fabs(dc) < min_dc)
  {
    dc = min_dc * (dc / fabs(dc));
  }
  if (dc > current_dc + max_acc_dc * last_loop_time)
  {
    dc = current_dc + max_acc_dc * last_loop_time;
  }
  else if (dc < current_dc - max_acc_dc * last_loop_time)
  {
    dc = current_dc - max_acc_dc * last_loop_time;
  }
  dc_out = fabs(dc);
  analogWrite(enaPin, dc_out);
  if (dc > 0)
  {
    digitalWrite(in1Pin, LOW);
    digitalWrite(in2Pin, HIGH);
  }
  else if (dc < 0)
  {
    digitalWrite(in1Pin, HIGH);
    digitalWrite(in2Pin, LOW);
  }
  current_dc = dc;
}

/*
Calculate distance from encoder position
*/
float get_distance(long encoder_pos)
{
  return encoder_pos * counter_to_mm;
}

/*
Estimates dc for a given value in mm/s
50 dc = 300 mm/s
*/
int estimate_dc(float speed)
{
  float distance = get_distance(encoder_pos);
  float dc = speed / distance * max_dc; // todo: check if this is correct
  if (dc > max_dc)
  {
    dc = max_dc;
  }
  else if (dc < min_dc)
  {
    dc = min_dc;
  }
  return dc;
}

/*
PID controlled speed function
*/
void pid_speed()
{
  target_distance += current_speed * last_loop_time;
  float error = target_distance - current_distance;
  if (enable_dc)
  {
    pid_integral += error * last_loop_time; // ! somwhow the sign changes when ki is too high
  }
  pid_before_checking = pid_integral;
  if (pid_integral != 0 && fabs(pid_integral) > i_max * current_speed)
  {
    pid_integral = i_max * (pid_integral / fabs(pid_integral)) * current_speed;
  }
  float additional_speed = Kp * error + Ki * pid_integral + Kd * (error - last_error) / last_loop_time;
  set_dc(additional_speed);
  last_error = error;
}

/*
Loop function that runs each time the loop is called
This function takes care of acceleration
*/
void drive_loop()
{
  if (target_speed - current_speed != 0 && fabs(target_speed - current_speed) > 1)
  {
    current_speed += (float)(target_speed - current_speed) / fabs(target_speed - current_speed) * acc * last_loop_time;
  }
  else
  {
    current_speed = target_speed;
  }
  if (enable_dc || hold_dc)
  {
    pid_speed();
  }
  else
  {
    target_distance = current_distance;
  }
  measured_speed = (current_distance - last_distance) / last_loop_time; // approximate speed in mm/s todo: average over multiple loops
}

/*
Function to set the acceleration speed
*/

/*
Emergency stop, shuts down speed directly to 0
*/
void emergency_stop(bool hold = false)
{
  target_speed = 0;
  target_distance = current_distance;
  analogWrite(enaPin, 0);
  digitalWrite(in1Pin, LOW);
  digitalWrite(in2Pin, LOW);
  current_dc = 0;
  current_speed = 0;
  enable_dc = false;
  if (!hold)
  {
    pid_integral = 0;
    last_error = 0;
  }
}

/*
Function to set the speed
*/
void set_speed(int speed)
{
  if (speed == 0)
  {
    emergency_stop();
  }
  else
  {
    enable_dc = true;
    target_speed = speed;
  }
}

void drive(int speed, bool force = false)
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
  if (fabs(speed - current_speed) > 1 && !force)
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
  analogWrite(enaPin, fabs(current_speed));
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

  switch (cmd[0])
  {
  case 'd':
    _set_speed = value;
    break;
  case 's':
    set_degree = value;
    break;
  // case 'e':
  //   Serial.println(encoder_pos);
  //   break;
  case 'c':
    dc_to_set_temp = value;
    break;
  case 'p':
    emergency_stop();
    break;
  case 'i':
    set_speed(value);
    break;
  case 'q':
    Kp = value / 10.;
    break;
  case 'w':
    Ki = value / 100.;
    break;
  case 'e':
    Kd = value / 10.;
    break;
  }
  // if (cmd[0] == 'd')
  // {
  //   _set_speed = value;
  // }
  // else if (cmd[0] == 's')
  // {
  //   set_degree = value;
  // }
  // else if (cmd[0] == 'e')
  // {
  //   Serial.println(encoder_pos);
  // }
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
}

void loop()
{
  current_time = micros();
  current_distance = get_distance(encoder_pos);
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
  drive(en_state ? _set_speed : 0);
  checkEnable();

  drive_loop();
  // set_dc(dc_to_set_temp);
  if (current_time - last_status_time > 200000)
  {
    last_status_time = current_time;
    // Serial.print("time passed (ms): ");
    // Serial.print(last_loop_time * 1000);
    // Serial.print(" encoder_pos: ");
    // Serial.print(get_distance(encoder_pos));
    Serial.print(" target_speed: ");
    Serial.print(target_speed);
    Serial.print(" current_speed: ");
    Serial.print(current_speed);
    Serial.print(" target_distance: ");
    Serial.print(target_distance);
    // Serial.print(" measured_speed: ");
    // Serial.print(measured_speed);
    // Serial.print(" dc_to_set_temp: ");
    // Serial.print(dc_to_set_temp);
    Serial.print(" current_dc: ");
    Serial.print(current_dc);
    Serial.print(" kp: ");
    Serial.print(Kp);
    Serial.print(" ki: ");
    Serial.print(Ki);
    Serial.print(" kd: ");
    Serial.print(Kd);
    Serial.print(" dc: ");
    Serial.print(current_dc);
    Serial.print(" error: ");
    Serial.print(target_distance - current_distance);
    Serial.print(" pid_integral: ");
    Serial.print(pid_integral);
    Serial.print(" dc_out: ");
    Serial.print(dc_out);
    Serial.print(" pid_before_checking: ");
    Serial.print(pid_before_checking);
    Serial.print("\r\n");
  }

  last_time = current_time;
  last_distance = current_distance;
}
