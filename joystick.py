import pygame
import tkinter as tk
from tkinter import ttk
import threading
import time
import asyncio
import websockets
import keyboard  # für Tastenbelegungen
import json
import os

# ---------------------------------------------------------------------------
# Joystick-Profile / Achsen-Zuordnung
#
# Der FiveM-Client (client.lua) erwartet folgende logische Achsenreihenfolge:
#   [0] X  -> Roll      (control 107)
#   [1] Y  -> Pitch     (control 110)
#   [2] Z  -> Ruder     (control 89/90)
#   [3] R  -> Schub     (control 87/88)
#
# Verschiedene Joysticks liefern die physischen Achsen in anderer Reihenfolge.
# axis_map ordnet jeder logischen Achse eine physische pygame-Achse zu:
#   axis_map[logischer_index] = physischer_pygame_achsen_index
# ---------------------------------------------------------------------------
AXIS_LABELS = ["X / Roll", "Y / Pitch", "Z / Ruder", "R / Schub"]
DEFAULT_AXIS_MAP = [0, 1, 2, 3]

JOYSTICK_PROFILES = {
    # Thrustmaster T.Flight Stick X:
    #   Stick X/Y wie Standard, aber Schubhebel auf physischer Achse 2 und
    #   Twist-Ruder auf Achse 3 -> Z und R sind getauscht.
    "T.Flight Stick X": {
        "axis_map": [0, 1, 3, 2],
        "reverse": [False, False, False, False],
    },
    # Thrustmaster T16000M (urspruengliches Geraet): direkte Zuordnung.
    "T16000M": {
        "axis_map": [0, 1, 2, 3],
        "reverse": [False, False, False, False],
    },
}


def select_profile(name):
    """Passendes Profil anhand des Joystick-Namens auswaehlen."""
    if not name:
        return None
    lname = name.lower()
    for key, profile in JOYSTICK_PROFILES.items():
        if key.lower() in lname:
            return key, profile
    return None


class JoystickGUI:
    def __init__(self):
        self.running = True
        # Eigene Event-Loop erstellen (laeuft in eigenem Thread)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.ws = None

        self.root = tk.Tk()
        self.root.title("Joystick Monitor")
        self.root.geometry("640x880")

        # Variablen fuer Tastenbelegung
        self.waiting_for_keyboard = False
        self.waiting_for_joystick = False
        self.temp_keyboard_key = None
        self.key_bindings = {}  # {joystick_button: keyboard_key}
        self.keyboard_state = {}  # Status der Tastatur-Tasten
        self.command_bindings = {}  # {joystick_button: command}
        self.waiting_for_command = False
        self.temp_command = None

        # Achsen-Zuordnung
        self.axis_map = list(DEFAULT_AXIS_MAP)   # logisch -> physische Achse
        self.user_axis_map = False               # True = vom Nutzer/Config angepasst
        self.axis_combos = []                    # Zuordnungs-Dropdowns
        self.current_joystick_name = ""

        # Schieberegler erstellen
        self.sliders = []
        self.labels = []
        self.reverse_buttons = []
        self.axis_reversed = [False] * 4  # Umkehrstatus je Achse
        axis_names = ["X-Achse (Roll)", "Y-Achse (Pitch)", "Z-Achse (Ruder)", "R-Achse (Schub)"]

        for i, name in enumerate(axis_names):
            label = ttk.Label(self.root, text=name)
            label.grid(row=i, column=0, padx=5, pady=5)

            slider = ttk.Scale(
                self.root,
                from_=-1.0,
                to=1.0,
                orient="horizontal",
                length=200
            )
            slider.grid(row=i, column=1, padx=5, pady=5)
            slider.set(0)

            value_label = ttk.Label(self.root, text="0.00")
            value_label.grid(row=i, column=2, padx=5, pady=5)

            # Umkehr-Button (dreht die Richtung der Achse um)
            reverse_button = ttk.Button(
                self.root,
                text="Normal",
                command=lambda x=i: self.toggle_reverse(x)
            )
            reverse_button.grid(row=i, column=3, padx=5, pady=5)

            self.sliders.append(slider)
            self.labels.append(value_label)
            self.reverse_buttons.append(reverse_button)

        # Status der gedrueckten Tasten
        self.button_label = ttk.Label(self.root, text="Keine Taste gedrueckt")
        self.button_label.grid(row=4, column=0, columnspan=3, pady=10)

        # HAT / Coolie-Hat Anzeige
        self.hat_frame = ttk.LabelFrame(self.root, text="HAT / Coolie-Hat")
        self.hat_frame.grid(row=5, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")

        self.hat_label = ttk.Label(self.hat_frame, text="Richtung: Mitte")
        self.hat_label.grid(row=0, column=0, padx=5, pady=5)

        self.hat_canvas = tk.Canvas(self.hat_frame, width=100, height=100)
        self.hat_canvas.grid(row=1, column=0, padx=5, pady=5)
        self._draw_hat_indicator((0, 0))

        self.running = True

        # Tastenbelegung
        self.binding_frame = ttk.LabelFrame(self.root, text="Tastenbelegung")
        self.binding_frame.grid(row=6, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.binding_label = ttk.Label(self.binding_frame, text="Keine Tastenbelegung aktiv")
        self.binding_label.grid(row=0, column=0, padx=5, pady=5)

        self.binding_button = ttk.Button(
            self.binding_frame,
            text="Neue Taste belegen",
            command=self.start_key_binding
        )
        self.binding_button.grid(row=0, column=1, padx=5, pady=5)

        # Befehl-Belegung
        self.command_binding_frame = ttk.LabelFrame(self.root, text="Befehl belegen")
        self.command_binding_frame.grid(row=7, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.command_label = ttk.Label(self.command_binding_frame, text="Kein Befehl belegt")
        self.command_label.grid(row=0, column=0, padx=5, pady=5)

        self.command_entry = ttk.Entry(self.command_binding_frame)
        self.command_entry.grid(row=0, column=1, padx=5, pady=5)

        self.command_bind_button = ttk.Button(
            self.command_binding_frame,
            text="Befehl belegen",
            command=self.start_command_binding
        )
        self.command_bind_button.grid(row=0, column=2, padx=5, pady=5)

        # Geraet & Achsen-Zuordnung
        self._build_device_frame()

        # Button-Leiste
        self.button_frame = ttk.Frame(self.root)
        self.button_frame.grid(row=10, column=0, columnspan=4, pady=10)

        self.connect_button = ttk.Button(
            self.button_frame,
            text="FiveM-Verbindung erlauben",
            command=self.connect_to_fivem
        )
        self.connect_button.grid(row=0, column=0, padx=10)

        self.zr_swap_button = ttk.Button(
            self.button_frame,
            text="ZR-Tausch: Aus",
            command=self.toggle_zr_swap
        )
        self.zr_swap_button.grid(row=0, column=1, padx=10)
        self.zr_swapped = False

        self.save_config_button = ttk.Button(
            self.button_frame,
            text="Konfiguration speichern",
            command=self.save_config
        )
        self.save_config_button.grid(row=0, column=2, padx=10)

        self.exit_button = ttk.Button(
            self.button_frame,
            text="Beenden",
            command=self.quit_application
        )
        self.exit_button.grid(row=0, column=3, padx=10)

        # Konfiguration laden (nach Erstellen aller Bedienelemente)
        self.load_config()

    def _build_device_frame(self):
        """Geraete-Info + Achsen-Zuordnung."""
        self.device_frame = ttk.LabelFrame(
            self.root, text="Geraet & Achsen-Zuordnung"
        )
        self.device_frame.grid(row=8, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.device_name_label = ttk.Label(
            self.device_frame, text="Joystick: (nicht erkannt)"
        )
        self.device_name_label.grid(row=0, column=0, columnspan=4, padx=5, pady=2, sticky="w")

        self.raw_axes_label = ttk.Label(self.device_frame, text="Rohe Achsen: -")
        self.raw_axes_label.grid(row=1, column=0, columnspan=4, padx=5, pady=2, sticky="w")

        hint = ttk.Label(
            self.device_frame,
            text="Bewege eine Achse und schau, welche Nummer oben reagiert. Dann unten zuordnen."
        )
        hint.grid(row=2, column=0, columnspan=4, padx=5, pady=2, sticky="w")

        # Pro logischer Achse ein Dropdown zur Wahl der physischen Achse
        for i, lab in enumerate(AXIS_LABELS):
            ttk.Label(self.device_frame, text=lab).grid(
                row=3 + i, column=0, padx=5, pady=2, sticky="w"
            )
            combo = ttk.Combobox(
                self.device_frame, width=5, state="readonly",
                values=[str(n) for n in range(8)]
            )
            combo.set(str(self.axis_map[i]))
            combo.grid(row=3 + i, column=1, padx=5, pady=2)
            combo.bind("<<ComboboxSelected>>", lambda e, idx=i: self.on_axis_map_change(idx))
            ttk.Label(self.device_frame, text="<- physische Achse").grid(
                row=3 + i, column=2, padx=5, pady=2, sticky="w"
            )
            self.axis_combos.append(combo)

    def update_buttons(self, buttons):
        """Tastenstatus aktualisieren und Tastenbelegungen verarbeiten."""
        # Zuerst losgelassene Tastatur-Tasten behandeln
        for button_num in self.keyboard_state:
            if str(button_num) not in buttons and self.keyboard_state[button_num]:
                key = self.key_bindings[button_num]
                keyboard.release(key)
                self.keyboard_state[button_num] = False

        # Befehl-Belegung verarbeiten
        if self.waiting_for_command and len(buttons) == 1:
            button_num = int(buttons[0])
            self.command_bindings[button_num] = self.temp_command
            self.command_label.config(text=f"Belegt: Taste {button_num} -> {self.temp_command}")
            self.waiting_for_command = False
            self.temp_command = None
            self.command_bind_button.config(state="normal")
            return

        if buttons:
            self.button_label.config(text=f"Gedrueckte Tasten: {', '.join(buttons)}")

            # Tastenbelegung verarbeiten
            if self.waiting_for_joystick and len(buttons) == 1:
                self.complete_binding(buttons[0])
                return

            # Belegte Tastatur-Tasten simulieren
            for button in buttons:
                button_num = int(button)
                if button_num in self.key_bindings:
                    key = self.key_bindings[button_num]
                    if not self.keyboard_state.get(button_num, False):
                        keyboard.press(key)
                        self.keyboard_state[button_num] = True

                # Belegten Befehl senden
                if button_num in self.command_bindings:
                    command = self.command_bindings[button_num]
                    if self.ws:
                        data = f"cmd:{command}"
                        asyncio.run_coroutine_threadsafe(self.ws.send(data), self.loop)
        else:
            self.button_label.config(text="Keine Taste gedrueckt")

    def _draw_hat_indicator(self, hat_value):
        """HAT-Richtungsanzeige zeichnen."""
        self.hat_canvas.delete("all")

        # Hintergrund-Kreuz
        self.hat_canvas.create_line(50, 10, 50, 90, fill="gray")
        self.hat_canvas.create_line(10, 50, 90, 50, fill="gray")

        x, y = hat_value
        center_x = 50 + (x * 20)
        center_y = 50 - (y * 20)

        self.hat_canvas.create_oval(center_x-5, center_y-5,
                                  center_x+5, center_y+5,
                                  fill="red")

    def update_hat(self, hat_value):
        """HAT-Anzeige aktualisieren."""
        x, y = hat_value
        directions = []
        if y > 0: directions.append("Oben")
        if y < 0: directions.append("Unten")
        if x < 0: directions.append("Links")
        if x > 0: directions.append("Rechts")

        direction_text = "Mitte" if not directions else " + ".join(directions)
        self.hat_label.config(text=f"Richtung: {direction_text}")

        self._draw_hat_indicator(hat_value)

    def display(self, message):
        # Statusmeldung (z. B. "Joystick erkannt") im Fenster anzeigen
        label = ttk.Label(self.root, text=message)
        label.grid(row=11, column=0, columnspan=3, pady=10)

    # ------------------------------------------------------------------
    # Geraet / Achsen-Zuordnung (aus dem Joystick-Thread via root.after)
    # ------------------------------------------------------------------
    def set_device_name(self, name, numaxes, numbuttons, numhats):
        """Erkannten Joystick anzeigen."""
        self.current_joystick_name = name
        self.device_name_label.config(
            text=f"Joystick: {name}  |  Achsen:{numaxes}  Tasten:{numbuttons}  HATs:{numhats}"
        )

    def update_raw_axes(self, raw):
        """Live-Werte aller physischen Achsen anzeigen (zum Kalibrieren)."""
        txt = "Rohe Achsen:  " + "   ".join(f"{i}:{v:+.2f}" for i, v in enumerate(raw))
        self.raw_axes_label.config(text=txt)

    def update_combo_ranges(self, count):
        """Auswahlbereich der Dropdowns an tatsaechliche Achsenzahl anpassen."""
        vals = [str(n) for n in range(max(count, 4))]
        for combo in self.axis_combos:
            combo.config(values=vals)

    def apply_axis_map(self, axis_map, reverse=None, axis_count=None):
        """Ein Profil anwenden (Zuordnung + Umkehr) und Anzeige aktualisieren."""
        self.axis_map = list(axis_map)
        if axis_count:
            self.update_combo_ranges(axis_count)
        for i, combo in enumerate(self.axis_combos):
            if i < len(self.axis_map):
                combo.set(str(self.axis_map[i]))
        if reverse is not None:
            self.axis_reversed = list(reverse)
            for i, rev in enumerate(self.axis_reversed):
                self.reverse_buttons[i].config(text="Umgekehrt" if rev else "Normal")

    def on_axis_map_change(self, logical_index):
        """Nutzer hat die physische Achse einer logischen Achse geaendert."""
        try:
            phys = int(self.axis_combos[logical_index].get())
        except (ValueError, IndexError):
            return
        if logical_index < len(self.axis_map):
            self.axis_map[logical_index] = phys
        self.user_axis_map = True

    def connect_to_fivem(self):
        """Verbindung zu FiveM herstellen (WebSocket-Server starten)."""
        try:
            self.loop_thread = threading.Thread(target=self._run_event_loop)
            self.loop_thread.daemon = True
            self.loop_thread.start()

            time.sleep(0.1)

            async def start_server():
                self.ws_server = await websockets.serve(
                    lambda ws: self._handle_websocket(ws),  # lambda entfernt das path-Argument
                    '127.0.0.1',
                    11556
                )

            future = asyncio.run_coroutine_threadsafe(
                start_server(),
                self.loop
            )
            future.result()  # auf Serverstart warten

            self.connect_button.config(text="Warte auf Verbindung", state="disabled")

        except Exception as e:
            print(f"Start fehlgeschlagen: {str(e)}")

    async def _handle_websocket(self, websocket):
        """WebSocket-Verbindung behandeln."""
        try:
            self.ws = websocket
            self.root.after(0, lambda: (
                self.connect_button.config(text="Verbunden", state="disabled"),
            ))
            print("Client verbunden")

            message = await websocket.recv()
            print(f"Nachricht erhalten: {message}")

            if message == 'connect':
                print("Starte Datenuebertragung")
                self.send_thread = threading.Thread(target=self._send_joystick_data)
                self.send_thread.daemon = True
                self.send_thread.start()

            while self.running:
                await asyncio.sleep(0.1)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"Verbindung geschlossen: {e}")
            self.root.after(0, lambda: (
                self.connect_button.config(text="FiveM-Verbindung erlauben", state="normal"),
            ))
        except Exception as e:
            print(f"Verbindungsfehler: {str(e)}")
            self.root.after(0, lambda: (
                self.connect_button.config(text="FiveM-Verbindung erlauben", state="normal"),
                self.display(f"Verbindungsfehler: {str(e)}")
            ))
        finally:
            self.ws = None

    def toggle_reverse(self, axis_index):
        """Umkehrstatus einer Achse umschalten."""
        self.axis_reversed[axis_index] = not self.axis_reversed[axis_index]
        button_text = "Normal" if not self.axis_reversed[axis_index] else "Umgekehrt"
        self.reverse_buttons[axis_index].config(text=button_text)

    def update_values(self, values):
        """Achsenwerte aktualisieren (mit Umkehr und ZR-Tausch)."""
        if self.zr_swapped and len(values) >= 4:
            values[2], values[3] = values[3], values[2]

        for i, value in enumerate(values):
            if i < len(self.sliders):
                actual_value = -value if self.axis_reversed[i] else value
                self.sliders[i].set(actual_value)
                self.labels[i].config(text=f"{actual_value:.2f}")

    def toggle_zr_swap(self):
        """Z- und R-Achse tauschen."""
        self.zr_swapped = not self.zr_swapped
        button_text = "ZR-Tausch: An" if self.zr_swapped else "ZR-Tausch: Aus"
        self.zr_swap_button.config(text=button_text)

    def _send_joystick_data(self):
        """Joystick-Daten senden (Umkehr/ZR sind bereits in den Reglern enthalten)."""
        while self.running and self.ws:
            try:
                values = []
                for i, slider in enumerate(self.sliders):
                    value = slider.get()
                    values.append(value)

                data = ','.join(f"{v:.3f}" for v in values)
                asyncio.run_coroutine_threadsafe(self.ws.send(data), self.loop)
                time.sleep(1/60)

            except Exception as e:
                print(f"Fehler beim Senden: {str(e)}")
                break

    def _run_event_loop(self):
        """Event-Loop im eigenen Thread ausfuehren."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def quit_application(self):
        """Programm beenden."""
        self.running = False

        if self.ws:
            asyncio.run_coroutine_threadsafe(
                self.ws.close(),
                self.loop
            )

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.loop_thread:
                self.loop_thread.join(timeout=1.0)
            self.loop.close()

        self.root.quit()

    def start_key_binding(self):
        """Tastenbelegung starten."""
        self.waiting_for_keyboard = True
        self.binding_label.config(text="Druecke die zu belegende Tastatur-Taste...")
        self.binding_button.config(state="disabled")

        self.root.bind('<Key>', self.on_keyboard_press)

    def on_keyboard_press(self, event):
        """Tastatur-Tastendruck behandeln."""
        if self.waiting_for_keyboard:
            self.temp_keyboard_key = event.keysym
            self.waiting_for_keyboard = False
            self.waiting_for_joystick = True
            self.binding_label.config(text=f"Taste erkannt: {event.keysym}\nDruecke jetzt die Joystick-Taste...")
            self.root.unbind('<Key>')

    def complete_binding(self, joystick_button):
        """Tastenbelegung abschliessen."""
        if self.waiting_for_joystick and self.temp_keyboard_key:
            try:
                keyboard.parse_hotkey(self.temp_keyboard_key)

                self.key_bindings[int(joystick_button)] = self.temp_keyboard_key
                self.binding_label.config(
                    text=f"Belegt: Joystick-Taste {joystick_button} -> Tastatur {self.temp_keyboard_key}"
                )
            except ValueError:
                self.binding_label.config(
                    text=f"Fehlgeschlagen: ungueltige Taste {self.temp_keyboard_key}"
                )
            finally:
                self.waiting_for_joystick = False
                self.temp_keyboard_key = None
                self.binding_button.config(state="normal")

    def start_command_binding(self):
        """Befehl-Belegung starten."""
        command = self.command_entry.get()
        if not command:
            return

        self.waiting_for_command = True
        self.temp_command = command
        self.command_label.config(text="Druecke die zu belegende Joystick-Taste...")
        self.command_bind_button.config(state="disabled")

    def save_config(self):
        """Konfiguration in Datei speichern."""
        config = {
            'axis_map': self.axis_map,
            'axis_reversed': self.axis_reversed,
            'zr_swapped': self.zr_swapped,
            'key_bindings': {str(k): v for k, v in self.key_bindings.items()},
            'command_bindings': {str(k): v for k, v in self.command_bindings.items()}
        }

        try:
            with open('joystick_config.json', 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.display("Konfiguration gespeichert.")
        except Exception as e:
            print(f"Speichern fehlgeschlagen: {str(e)}")

    def load_config(self):
        """Konfiguration aus Datei laden."""
        if not os.path.exists('joystick_config.json'):
            return

        try:
            with open('joystick_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)

            # Achsen-Zuordnung
            axis_map = config.get('axis_map')
            if axis_map and len(axis_map) == 4:
                self.axis_map = [int(x) for x in axis_map]
                self.user_axis_map = True
                for i, combo in enumerate(self.axis_combos):
                    combo.set(str(self.axis_map[i]))

            # Umkehr-Einstellungen
            self.axis_reversed = config.get('axis_reversed', [False] * 4)
            for i, reversed_state in enumerate(self.axis_reversed):
                self.reverse_buttons[i].config(text="Umgekehrt" if reversed_state else "Normal")

            # ZR-Tausch
            self.zr_swapped = config.get('zr_swapped', False)
            if self.zr_swapped:
                self.zr_swap_button.config(text="ZR-Tausch: An")

            # Tastenbelegungen
            key_bindings = config.get('key_bindings', {})
            self.key_bindings = {int(k): v for k, v in key_bindings.items()}

            # Befehl-Belegungen
            command_bindings = config.get('command_bindings', {})
            self.command_bindings = {int(k): v for k, v in command_bindings.items()}

        except Exception as e:
            print(f"Laden fehlgeschlagen: {str(e)}")


def joystick_thread(gui):
    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        print("Kein Joystick erkannt")
        gui.root.after(0, lambda: gui.device_name_label.config(
            text="Joystick: NICHT ERKANNT - Stick anschliessen und Programm neu starten"
        ))
        return

    joystick = pygame.joystick.Joystick(0)
    joystick.init()

    name = joystick.get_name()
    numaxes = joystick.get_numaxes()
    numbuttons = joystick.get_numbuttons()
    numhats = joystick.get_numhats()

    print(f"Joystick erkannt: {name} (Achsen:{numaxes} Tasten:{numbuttons} HATs:{numhats})")
    gui.root.after(0, gui.set_device_name, name, numaxes, numbuttons, numhats)

    # Profil automatisch waehlen (sofern keine eigene/gespeicherte Zuordnung)
    selected = select_profile(name)
    if selected and not gui.user_axis_map:
        key, profile = selected
        print(f"Profil automatisch angewendet: {key}")
        gui.root.after(0, gui.apply_axis_map,
                       profile["axis_map"], profile["reverse"], numaxes)
    else:
        gui.root.after(0, gui.update_combo_ranges, numaxes)

    try:
        while gui.running:
            pygame.event.pump()

            # HAT lesen
            if numhats > 0:
                hat_value = joystick.get_hat(0)
                gui.root.after(0, gui.update_hat, hat_value)

            # Alle physischen Achsen roh lesen
            raw = [joystick.get_axis(i) for i in range(numaxes)]
            gui.root.after(0, gui.update_raw_axes, raw)

            # Physische Achsen ueber die Zuordnung in X,Y,Z,R umsetzen
            axes_values = []
            for logical in range(4):
                phys = gui.axis_map[logical] if logical < len(gui.axis_map) else logical
                axes_values.append(raw[phys] if 0 <= phys < len(raw) else 0.0)

            # Tasten lesen
            pressed_buttons = [str(i) for i in range(numbuttons) if joystick.get_button(i)]

            gui.root.after(0, gui.update_values, axes_values)
            gui.root.after(0, gui.update_buttons, pressed_buttons)

            time.sleep(0.1)

    finally:
        pygame.quit()


if __name__ == "__main__":
    gui = JoystickGUI()

    thread = threading.Thread(target=joystick_thread, args=(gui,))
    thread.daemon = True
    thread.start()
    print("Joystick Monitor gestartet")

    try:
        gui.root.mainloop()
    finally:
        gui.running = False
        if hasattr(gui, 'loop_thread') and gui.loop_thread is not None:
            gui.loop_thread.join(timeout=1.0)
        if thread is not None:
            thread.join(timeout=1.0)
