#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ---------- Configuration ----------
const char* ssid = "QuizNetwork";
const char* password = "quiz12345";
const char* serverURL = "https://technomath-6f93.onrender.com/receive_data";  // Updated URL
const String assignedOption = "A";  // Change to B/C/D for other devices

// ---------- Hardware Pins ----------
#define RST_PIN     22
#define SS_PIN      21
#define LCD_ADDR    0x27
#define LCD_COLS    16
#define LCD_ROWS    2

// ---------- Hardware Objects ----------
MFRC522 mfrc522(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);

// ---------- State Management ----------
unsigned long lastDisplayTime = 0;
unsigned long lastCardRead = 0;
bool displayingStudent = false;
const unsigned long CARD_READ_COOLDOWN = 2000;  // 2 seconds between reads
const unsigned long DISPLAY_TIMEOUT = 5000;     // 5 seconds display time

// ---------- Student Database ----------
struct Student {
  String tagID;
  String name;
};

const Student students[] = {
  {"b358f627", "Student 01"},
  {"f3c7ece1", "Student 02"},
  {"13dadcd9", "Student 03"},
  {"b3b27bda", "Student 04"},
  {"0e317ee2", "Student 05"},
  {"bc8e973f", "Student 06"},
  {"0684973f", "Student 07"},
  {"051973f0", "Student 08"},
  {"0b325a14", "Student 09"},
  {"c5d0963f", "Student 10"}
};

const int STUDENT_COUNT = sizeof(students) / sizeof(students[0]);

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

String getStudentName(const String& tagID) {
  for (int i = 0; i < STUDENT_COUNT; i++) {
    if (students[i].tagID == tagID) {
      return students[i].name;
    }
  }
  return "Unknown";
}

String urlEncode(const String& str) {
  String encoded = "";
  for (unsigned int i = 0; i < str.length(); i++) {
    char c = str.charAt(i);
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
      encoded += c;
    } else {
      encoded += '%';
      encoded += String(c, HEX).toUpperCase();
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
bool sendToServer(const String& studentName, const String& option) {
  if (WiFi.status() != WL_CONNECTED) {
    displayError("WiFi Disconnected");
    return false;
  }

  HTTPClient http;
  http.setTimeout(15000);  // Increased timeout for HTTPS
  http.setInsecure();      // Skip SSL certificate verification for simplicity
  
  String url = String(serverURL) + 
               "?student=" + urlEncode(studentName) + 
               "&option=" + urlEncode(option);
  
  Serial.println("Sending to: " + url);
  
  http.begin(url);
  int httpCode = http.GET();
  
  if (httpCode > 0) {
    String response = http.getString();
    Serial.printf("HTTP Code: %d\n", httpCode);
    Serial.println("Response: " + response);
    
    if (httpCode == 200) {
      displayMessage("Success!", studentName);
      http.end();
      return true;
    } else {
      displayError("Server Error: " + String(httpCode));
    }
  } else {
    displayError("Connection Failed");
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
  Wire.begin(18, 19);  // SDA, SCL
  lcd.begin();
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
  
  unsigned long wifiTimeout = millis() + 30000;  // 30 second timeout
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
  
  // Ready state
  displayMessage("Ready", "Pillar " + assignedOption);
  Serial.println("System ready. Waiting for RFID cards...");
}

void loop() {
  // Check for new RFID card
  if (mfrc522.PICC_IsNewCardPresent() && mfrc522.PICC_ReadCardSerial()) {
    unsigned long currentTime = millis();
    
    // Prevent rapid successive reads
    if (currentTime - lastCardRead < CARD_READ_COOLDOWN) {
      mfrc522.PICC_HaltA();
      mfrc522.PCD_StopCrypto1();
      return;
    }
    
    lastCardRead = currentTime;
    String tagID = readTagID();
    String studentName = getStudentName(tagID);
    
    Serial.println("Card detected: " + tagID + " → " + studentName);
    
    if (studentName == "Unknown") {
      displayMessage("Unknown Card", tagID);
      Serial.println("Unknown card: " + tagID);
    } else {
      displayMessage("Processing...", studentName);
      bool success = sendToServer(studentName, assignedOption);
      
      if (success) {
        lastDisplayTime = currentTime;
        displayingStudent = true;
      }
    }
    
    // Clean up RFID
    mfrc522.PICC_HaltA();
    mfrc522.PCD_StopCrypto1();
  }
  
  // Return to ready state after display timeout
  if (displayingStudent && millis() - lastDisplayTime > DISPLAY_TIMEOUT) {
    displayMessage("Ready", "Pillar " + assignedOption);
    displayingStudent = false;
  }
  
  // Check WiFi connection periodically
  static unsigned long lastWiFiCheck = 0;
  if (millis() - lastWiFiCheck > 30000) {  // Check every 30 seconds
    if (WiFi.status() != WL_CONNECTED) {
      displayMessage("Reconnecting...", "WiFi Lost");
      WiFi.reconnect();
    }
    lastWiFiCheck = millis();
  }
  
  delay(100);  // Small delay to prevent overwhelming the system
}