#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecure.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
// ---------- Configuration ----------
const char* ssid = "raghu";
const char* password = "12345678";
const char* serverURL = "https://technomath-6f93.onrender.com/receive_data";
const String assignedOption = "B";  // Change to B/C/D for other devices
// ---------- Hardware Pins ----------
#define RST_PIN   D1   // GPIO5 for RC522 reset
#define SS_PIN    D2   // GPIO4 for RC522 SDA(SS)
#define LCD_ADDR  0x27
#define LCD_COLS  16
#define LCD_ROWS  2
// ---------- Hardware Objects ----------
MFRC522 mfrc522(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);
// ---------- State Management ----------
unsigned long lastDisplayTime = 0;
unsigned long lastCardRead = 0;
bool displayingStudent = false;
const unsigned long CARD_READ_COOLDOWN = 2000;
const unsigned long DISPLAY_TIMEOUT = 3000;     // 3 sec display timeout
// ---------- Utility Functions ----------
String readTagID() {
  String tagID = "";
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    if (mfrc522.uid.uidByte[i] < 0x10) tagID += "0";
    tagID += String(mfrc522.uid.uidByte[i], HEX);
  }
  tagID.toLowerCase();
  return tagID;
}
String urlEncode(const String& str) {
  String encoded = "";
  for (unsigned int i = 0; i < str.length(); i++) {
    char c = str.charAt(i);
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      encoded += c;
    } else {
      encoded += '%';
      String hexStr = String(c, HEX);
      hexStr.toUpperCase();
      encoded += hexStr;
    }
  }
  return encoded;
}
void displayMessage(const String& line1, const String& line2 = "") {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1);
  if (line2.length() > 0) {
    lcd.setCursor(0, 1);
    lcd.print(line2);
  }
}
void displayError(const String& error) {
  displayMessage("Error:", error);
  Serial.println("Error: " + error);
}
// ---------- Network Functions ----------
bool sendToServer(const String& tagID, const String& option) {
  if (WiFi.status() != WL_CONNECTED) {
    displayError("WiFi Disconnected");
    return false;
  }
  WiFiClientSecure client;
  client.setInsecure();  // ignore SSL cert check
  HTTPClient http;
  http.setTimeout(15000);
  String url = String(serverURL) +
               "?tag_id=" + urlEncode(tagID) +
               "&option=" + urlEncode(option);
  Serial.println("Sending to: " + url);
  http.begin(client, url);
  int httpCode = http.GET();
  if (httpCode > 0) {
    String response = http.getString();
    Serial.printf("HTTP Code: %d\n", httpCode);
    Serial.println("Response: " + response);
    if (httpCode == 200) {
      displayMessage("Success!", "Tag: " + tagID);
      http.end();
      return true;
    } else {
      displayError("Server Err: " + String(httpCode));
    }
  } else {
    displayError("Conn Failed");
    Serial.println("HTTP Error: " + http.errorToString(httpCode));
  }
  http.end();
  return false;
}
// ---------- Main Functions ----------
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== Quiz Pillar System ===");
  Serial.println("Pillar: " + assignedOption);
  Serial.println("Server: " + String(serverURL));
  // Initialize I2C and LCD
  Wire.begin(D3, D4);   // SDA=D3, SCL=D4
  lcd.begin(LCD_COLS, LCD_ROWS);
  lcd.backlight();
  displayMessage("Initializing...", "Pillar " + assignedOption);
  // Initialize RFID
  SPI.begin();
  mfrc522.PCD_Init();
  if (!mfrc522.PCD_PerformSelfTest()) {
    displayError("RFID Init Failed");
    Serial.println("RFID initialization failed!");
  }
  // Connect to WiFi
  displayMessage("Connecting WiFi", "...");
  WiFi.begin(ssid, password);
  unsigned long wifiTimeout = millis() + 30000;
  while (WiFi.status() != WL_CONNECTED && millis() < wifiTimeout) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    Serial.println("IP: " + WiFi.localIP().toString());
    displayMessage("WiFi Connected", "Pillar " + assignedOption);
    delay(2000);
  } else {
    displayError("WiFi Failed");
    Serial.println("WiFi connection failed!");
  }
  displayMessage("Ready", "Pillar " + assignedOption);
  Serial.println("System ready. Waiting for RFID cards...");
}
void loop() {
  if (mfrc522.PICC_IsNewCardPresent() && mfrc522.PICC_ReadCardSerial()) {
    unsigned long currentTime = millis();
    if (currentTime - lastCardRead < CARD_READ_COOLDOWN) {
      mfrc522.PICC_HaltA();
      mfrc522.PCD_StopCrypto1();
      return;
    }
    lastCardRead = currentTime;
    String tagID = readTagID();
    Serial.println("Card detected: " + tagID);
    displayMessage("Processing...", "Tag: " + tagID);
    bool success = sendToServer(tagID, assignedOption);
    if (success) {
      lastDisplayTime = currentTime;
      displayingStudent = true;
    }
    mfrc522.PICC_HaltA();
    mfrc522.PCD_StopCrypto1();
  }
  if (displayingStudent && millis() - lastDisplayTime > DISPLAY_TIMEOUT) {
    displayMessage("Ready", "Pillar " + assignedOption);
    displayingStudent = false;
  }
  static unsigned long lastWiFiCheck = 0;
  if (millis() - lastWiFiCheck > 30000) {
    if (WiFi.status() != WL_CONNECTED) {
      displayMessage("Reconnecting...", "WiFi Lost");
      WiFi.reconnect();
    }
    lastWiFiCheck = millis();
  }
  delay(100);
}