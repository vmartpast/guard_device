// GUARD — firmware de la interfaz fisica (ESP32).
//
// Implementa el extremo ESP32 del protocolo definido en
// docs/PROTOCOLO_UART.md. Recibe estado y detecciones desde la
// Raspberry Pi por UART y las presenta en LCD, LEDs y buzzer.
//
// Placa: Freenove ESP32-WROVER. Core ESP32 3.x (API LEDC por pin).
//
// Decision de diseno (seccion 2 del protocolo): el ESP32 mantiene su
// propio temporizador de enlace. Si deja de recibir latidos, pasa a
// LINK_DOWN por si mismo. No depende de que la Pi se lo indique,
// porque el caso a cubrir es precisamente que la Pi no pueda indicar
// nada.

#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ---------------------------------------------------------------- pines
#define PIN_RX      33   // recibe de la Pi (su GPIO 4, pin fisico 7)
#define PIN_TX      32   // transmite a la Pi (su GPIO 5, pin fisico 29)
#define LED_VERDE   4
#define LED_AMBAR   5
#define LED_ROJO    18
#define BUZZER      19   // conexion directa, sin resistencia en serie
#define I2C_SDA     21
#define I2C_SCL     22

#define LCD_DIR     0x27
#define LCD_COLS    16
#define LCD_FILAS   2

// ------------------------------------------------------------ constantes
const uint32_t BAUD_ENLACE     = 115200;
const uint32_t TIMEOUT_LINK    = 6000;   // 3 latidos perdidos -> LINK_DOWN
const uint32_t DURACION_ALERTA = 10000;  // sin nuevas DET -> estado base
const uint8_t  MAX_TRAMA       = 96;

// Tono del buzzer segun potencia recibida. Un dron mas cercano produce
// RSSI mas alto (menos negativo) y tono mas agudo: el operador estima
// proximidad sin mirar la pantalla (requisito de aviso audible).
//
// El limite inferior es 1600 Hz y no un valor mas grave por la respuesta
// en frecuencia del transductor: por debajo de ~1,5 kHz la salida
// acustica cae tanto que la alerta resulta inaudible. El rango util se
// determino experimentalmente, no por criterio de diseno.
const int RSSI_MIN   = -85;
const int RSSI_MAX   = -30;
const int TONO_GRAVE = 1600;
const int TONO_AGUDO = 2600;

// --------------------------------------------------------------- estados
enum Estado { BOOT, LINK_DOWN, IDLE, DEGRADED, ALERT };

Estado   estado           = BOOT;
Estado   estadoBase       = LINK_DOWN;  // estado al expirar una alerta
uint32_t ultimoLatido     = 0;
uint32_t inicioAlerta     = 0;
uint32_t ultimaDeteccion  = 0;
bool     huboDeteccion    = false;
uint32_t uptimePi         = 0;

// Datos de la ultima deteccion, para la pantalla
float   detRssi      = 0;
bool    detTieneRssi = false;
String  detModelo    = "-";
float   detConf      = 0;

char    buffer[MAX_TRAMA];
uint8_t largoBuffer = 0;

LiquidCrystal_I2C lcd(LCD_DIR, LCD_COLS, LCD_FILAS);

// ------------------------------------------------------------- utilidades

// XOR de todos los bytes entre el prefijo y el '*'.
uint8_t calcularChecksum(const char* s, uint8_t desde, uint8_t hasta) {
  uint8_t cs = 0;
  for (uint8_t i = desde; i < hasta; i++) cs ^= (uint8_t)s[i];
  return cs;
}

void enviarTrama(const String& cuerpo) {
  uint8_t cs = 0;
  for (size_t i = 0; i < cuerpo.length(); i++) cs ^= (uint8_t)cuerpo[i];
  char hex[3];
  snprintf(hex, sizeof(hex), "%02X", cs);
  Serial2.print('<');
  Serial2.print(cuerpo);
  Serial2.print('*');
  Serial2.print(hex);
  Serial2.print('\n');
}

void enviarError(const char* codigo) {
  enviarTrama(String("ERR|") + codigo + "|");
}

// --------------------------------------------------------------- pantalla

void escribirLinea(uint8_t fila, const String& texto) {
  String t = texto;
  while (t.length() < LCD_COLS) t += ' ';
  if (t.length() > LCD_COLS) t = t.substring(0, LCD_COLS);
  lcd.setCursor(0, fila);
  lcd.print(t);
}

// Tiempo transcurrido en formato compacto: "12s", "4m", "2h".
String desdeUltimaDeteccion() {
  if (!huboDeteccion) return "--";
  uint32_t s = (millis() - ultimaDeteccion) / 1000;
  if (s < 60)   return String(s) + "s";
  if (s < 3600) return String(s / 60) + "m";
  return String(s / 3600) + "h";
}

void refrescarPantalla() {
  switch (estado) {
    case BOOT:
      escribirLinea(0, "GUARD");
      escribirLinea(1, "Iniciando...");
      break;

    case LINK_DOWN:
      escribirLinea(0, "ENLACE PERDIDO");
      escribirLinea(1, "Sin datos de Pi");
      break;

    case IDLE:
      escribirLinea(0, "GUARD  OPERATIVO");
      escribirLinea(1, "Sin detec.  " + desdeUltimaDeteccion());
      break;

    case DEGRADED:
      escribirLinea(0, "DETECTOR CAIDO");
      escribirLinea(1, "Sin vigilancia");
      break;

    case ALERT: {
      String l0 = "ALERTA";
      if (detTieneRssi) {
        String r = String((int)detRssi) + "dBm";
        while (l0.length() + r.length() < LCD_COLS) l0 += ' ';
        l0 += r;
      }
      escribirLinea(0, l0);

      String l1 = detModelo;
      if (l1.length() > 10) l1 = l1.substring(0, 10);
      String c = String(detConf, 2);
      while (l1.length() + c.length() < LCD_COLS) l1 += ' ';
      l1 += c;
      escribirLinea(1, l1);
      break;
    }
  }
}

// ------------------------------------------------------------ indicadores

void aplicarIndicadores() {
  digitalWrite(LED_VERDE, estado == IDLE);
  digitalWrite(LED_AMBAR, estado == DEGRADED);
  digitalWrite(LED_ROJO,  estado == ALERT || estado == LINK_DOWN);

  if (estado != ALERT) ledcWriteTone(BUZZER, 0);
}

void pitarDeteccion() {
  int tono = (TONO_GRAVE + TONO_AGUDO) / 2;
  if (detTieneRssi) {
    int r = constrain((int)detRssi, RSSI_MIN, RSSI_MAX);
    tono = map(r, RSSI_MIN, RSSI_MAX, TONO_GRAVE, TONO_AGUDO);
  }
  for (int i = 0; i < 3; i++) {
    ledcWriteTone(BUZZER, tono);
    delay(220);
    ledcWriteTone(BUZZER, 0);
    delay(120);
  }
}

// ------------------------------------------------------------ transiciones

void cambiarEstado(Estado nuevo) {
  if (estado == nuevo) return;
  estado = nuevo;
  aplicarIndicadores();
  refrescarPantalla();
  Serial.printf("[estado] -> %d\n", (int)nuevo);
}

// ---------------------------------------------------------------- parser

void procesarHB(const String& campos) {
  int sep = campos.indexOf('|');
  if (sep < 0) { enviarError("FMT"); return; }

  uptimePi = campos.substring(0, sep).toInt();
  String est = campos.substring(sep + 1);
  est.trim();

  ultimoLatido = millis();

  if (est == "OK")            estadoBase = IDLE;
  else if (est == "DEGRADED") estadoBase = DEGRADED;
  else if (est == "ERROR")    estadoBase = DEGRADED;
  else { enviarError("FMT"); return; }

  // Una alerta en curso no se interrumpe por un latido.
  if (estado != ALERT) cambiarEstado(estadoBase);
}

void procesarDET(const String& campos) {
  int p1 = campos.indexOf('|');
  int p2 = campos.indexOf('|', p1 + 1);
  if (p1 < 0 || p2 < 0) { enviarError("FMT"); return; }

  String sRssi = campos.substring(0, p1);
  detModelo    = campos.substring(p1 + 1, p2);
  detConf      = campos.substring(p2 + 1).toFloat();

  detTieneRssi = (sRssi != "-" && sRssi.length() > 0);
  if (detTieneRssi) detRssi = sRssi.toFloat();
  if (detModelo.length() == 0) detModelo = "-";

  ultimaDeteccion = millis();
  huboDeteccion   = true;
  inicioAlerta    = millis();

  cambiarEstado(ALERT);
  enviarTrama("ACK|DET|");   // permite a la Pi medir latencia hasta la alerta
  pitarDeteccion();
}

void procesarTrama(char* t, uint8_t largo) {
  if (t[0] != '>') return;                       // no dirigida a nosotros

  int posCS = -1;
  for (int i = largo - 1; i > 0; i--) {
    if (t[i] == '*') { posCS = i; break; }
  }
  if (posCS < 0 || largo - posCS < 3) { enviarError("FMT"); return; }

  uint8_t esperado = calcularChecksum(t, 1, posCS);
  uint8_t recibido = (uint8_t)strtol(t + posCS + 1, nullptr, 16);
  if (esperado != recibido) { enviarError("CS"); return; }

  String cuerpo = String(t).substring(1, posCS);

  // El protocolo cierra la lista de campos con un separador final.
  // Se elimina antes de trocear para que el ultimo campo no lo arrastre.
  if (cuerpo.endsWith("|")) cuerpo.remove(cuerpo.length() - 1);

  int sep = cuerpo.indexOf('|');
  if (sep < 0) { enviarError("FMT"); return; }

  String tipo  = cuerpo.substring(0, sep);
  String resto = cuerpo.substring(sep + 1);

  if (tipo == "HB")       procesarHB(resto);
  else if (tipo == "DET") procesarDET(resto);
  else if (tipo == "SYS") { /* telemetria: aun sin pantalla dedicada */ }
  else                    enviarError("FMT");
}

void leerEnlace() {
  while (Serial2.available()) {
    char c = Serial2.read();

    if (c == '\n') {
      if (largoBuffer > 0) {
        buffer[largoBuffer] = '\0';
        procesarTrama(buffer, largoBuffer);
        largoBuffer = 0;
      }
      continue;
    }
    if (c == '\r') continue;   // la Pi puede terminar con CRLF

    if (largoBuffer >= MAX_TRAMA - 1) {
      largoBuffer = 0;          // resincronizar: trama sobredimensionada
      enviarError("LEN");
      continue;
    }
    buffer[largoBuffer++] = c;
  }
}

// ------------------------------------------------------------------ setup

void setup() {
  Serial.begin(115200);
  Serial2.begin(BAUD_ENLACE, SERIAL_8N1, PIN_RX, PIN_TX);

  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMBAR, OUTPUT);
  pinMode(LED_ROJO, OUTPUT);
  ledcAttach(BUZZER, 2000, 8);

  Wire.begin(I2C_SDA, I2C_SCL);
  lcd.init();
  lcd.backlight();

  Serial.println("\nGUARD — interfaz fisica iniciada");
  Serial.printf("Enlace: GPIO%d RX / GPIO%d TX @ %lu\n",
                PIN_RX, PIN_TX, BAUD_ENLACE);

  cambiarEstado(BOOT);
  refrescarPantalla();
  delay(1200);

  // Sin latidos todavia: el estado honesto de partida es enlace perdido.
  ultimoLatido = 0;
  cambiarEstado(LINK_DOWN);
}

// ------------------------------------------------------------------- loop

void loop() {
  leerEnlace();

  uint32_t ahora = millis();

  // Expiracion de la alerta: volver al estado que indique el ultimo latido.
  if (estado == ALERT && ahora - inicioAlerta > DURACION_ALERTA) {
    cambiarEstado(estadoBase);
  }

  // Perdida de enlace. Tiene prioridad sobre cualquier otro estado,
  // incluida una alerta en curso: si no hay datos fiables, la interfaz
  // debe decirlo en lugar de seguir mostrando informacion antigua.
  if (ultimoLatido == 0 || ahora - ultimoLatido > TIMEOUT_LINK) {
    cambiarEstado(LINK_DOWN);
  }

  // Refresco periodico: actualiza el contador de tiempo en reposo.
  static uint32_t ultimoRefresco = 0;
  if (ahora - ultimoRefresco > 1000) {
    ultimoRefresco = ahora;
    if (estado == IDLE) refrescarPantalla();
  }
}
