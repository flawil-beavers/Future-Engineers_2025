#include <Arduino.h>
#include <Servo.h>
#include <PinChangeInterrupt.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_L3GD20_U.h>
#include <Wire.h>

Servo servo;

// pin definitions
const int servoPin = 2;
const int enaPin = 11;
const int in1Pin = 5;
const int in2Pin = 8;

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

int max_dc = 200;       // max duty cycle for motor driver
int min_dc = 25;        // min duty cycle for motor driver
float max_acc_dc = 255; // max acceleration duty cycle for motor driver (dc/s)
float current_dc = 0;   // current duty cycle for motor driver
float acc = 700;        // acceleration speed (mm/s^2)
bool enable_dc = true;  // enable dc motor
bool hold_dc = false;

// speed settings
float current_speed = 0;
int target_speed = 0; // target speed for the motor in mm/s
unsigned long acc_time = 20;
unsigned long last_acc_time = 0;

// steering settings
int middle = 97; // +55 -55
int degree_max = middle + 30;
int degree_min = middle - 30;
int current_degree = 0;
int set_degree = 0;

// PID
float target_distance = 0; // target encoder position in mm

float measured_speed = 0;   // measured speed in mm/s
float current_distance = 0; // current distance in mm
float last_distance = 0;    // last distance in mm
float Kp = 4.0;             // proportional gain for PID controller
float Ki = 3.0;             // integral gain for PID controller
float Kd = 1.0;             // derivative gain for PID controller
float i_max = 150.0;        // max integral value for PID controller
float pid_integral = 0.0;   // integral term for PID controller
float last_error = 0.0;     // last error for PID controller

// time variables
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
  pid_integral += error * last_loop_time; // ! somwhow the sign changes when ki is too high
  pid_before_checking = pid_integral;
  if (pid_integral != 0 && fabs(pid_integral) > i_max)
  {
    pid_integral = i_max * (pid_integral / fabs(pid_integral));
  }
  float speed = Kp * error + Ki * pid_integral + Kd * (error - last_error) / last_loop_time;
  set_dc(speed);
  last_error = error;
}

/*
Loop function that runs each time the loop is called
This function takes care of acceleration
*/
void drive_loop()
{
  if (fabs(target_speed - current_speed) > 1)
  {
    current_speed += (target_speed - current_speed) / fabs(target_speed - current_speed) * acc * last_loop_time;
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

void gyro_config()
{
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
}

void pid_config_print()
{
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
}
void gyro_config_print()
{
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
  value = atoi(beg);
  switch (cmd[0])
  {
  case 'd':
    set_speed(value);
    break;
  case 's':
    set_degree = value;
    break;
  case 'n':
    Serial.println(encoder_pos);
    break;
  case 'p':
    emergency_stop();
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
  checkEnable();

  drive_loop();
  update_gyro();

  last_time = current_time;
  last_distance = current_distance;
}