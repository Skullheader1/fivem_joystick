import pygame
import tkinter as tk
from tkinter import ttk
import threading
import time
import asyncio
import websockets
import keyboard  # 新增导入
import json
import os

# ---------------------------------------------------------------------------
# 摇杆配置 / Joystick profiles
#
# FiveM 客户端 (client.lua) 期望以下逻辑轴顺序 (logical axis order):
#   [0] X  -> 横滚 / Roll        (control 107)
#   [1] Y  -> 俯仰 / Pitch       (control 110)
#   [2] Z  -> 方向舵 / Rudder    (control 89/90)
#   [3] R  -> 油门 / Throttle    (control 87/88)
#
# 不同摇杆的物理轴顺序不一样，下面的 axis_map 把
# "逻辑轴 -> 物理 pygame 轴" 做一次映射。
# axis_map[logical_index] = physical_pygame_axis_index
# ---------------------------------------------------------------------------
AXIS_LABELS = ["X / Roll", "Y / Pitch", "Z / Rudder", "R / Throttle"]
DEFAULT_AXIS_MAP = [0, 1, 2, 3]

JOYSTICK_PROFILES = {
    # Thrustmaster T.Flight Stick X:
    #   摇杆 X/Y 顺序与默认相同，但油门杆在物理轴 2、扭转方向舵在物理轴 3，
    #   也就是把 Z 和 R 调换 (equivalent to the old "ZR swap").
    "T.Flight Stick X": {
        "axis_map": [0, 1, 3, 2],
        "reverse": [False, False, False, False],
    },
    # Thrustmaster T16000M (原始目标设备 / original target device): 直接映射.
    "T16000M": {
        "axis_map": [0, 1, 2, 3],
        "reverse": [False, False, False, False],
    },
}


def select_profile(name):
    """根据摇杆名称选择匹配的配置 / pick a profile by joystick name."""
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
        # 创建新的事件循环，并确保它在主线程中运行
        self.loop = asyncio.new_event_loop()
        self.loop_thread = None
        self.ws = None

        self.root = tk.Tk()
        self.root.title("摇杆监视器 / Joystick Monitor")
        self.root.geometry("600x860")

        # 添加按键绑定相关变量
        self.waiting_for_keyboard = False
        self.waiting_for_joystick = False
        self.temp_keyboard_key = None
        self.key_bindings = {}  # {joystick_button: keyboard_key}
        self.keyboard_state = {}  # 记录键盘按键状态
        self.command_bindings = {}  # {joystick_button: command}
        self.waiting_for_command = False
        self.temp_command = None

        # 轴映射相关 / axis mapping state
        self.axis_map = list(DEFAULT_AXIS_MAP)   # logical -> physical pygame axis
        self.user_axis_map = False               # True 表示用户/配置已自定义映射
        self.axis_combos = []                    # 映射下拉框 / mapping comboboxes
        self.current_joystick_name = ""


        # 创建滑块
        self.sliders = []
        self.labels = []
        self.reverse_buttons = []  # 新增反向按钮列表
        self.axis_reversed = [False] * 4  # 记录每个轴的反向状态
        axis_names = ["X轴", "Y轴", "Z轴", "R轴"]



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

            # 添加反向按钮
            reverse_button = ttk.Button(
                self.root,
                text="正向",
                command=lambda x=i: self.toggle_reverse(x)
            )
            reverse_button.grid(row=i, column=3, padx=5, pady=5)

            self.sliders.append(slider)
            self.labels.append(value_label)
            self.reverse_buttons.append(reverse_button)

        # 按钮状态标签
        self.button_label = ttk.Label(self.root, text="没有按钮被按下")
        self.button_label.grid(row=4, column=0, columnspan=3, pady=10)

         # 添加HAT状态显示
        self.hat_frame = ttk.LabelFrame(self.root, text="苦力帽状态 / HAT")
        self.hat_frame.grid(row=5, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")

        # 创建方向指示标签
        self.hat_label = ttk.Label(self.hat_frame, text="当前方向: 中央")
        self.hat_label.grid(row=0, column=0, padx=5, pady=5)

        # 创建方向显示画布
        self.hat_canvas = tk.Canvas(self.hat_frame, width=100, height=100)
        self.hat_canvas.grid(row=1, column=0, padx=5, pady=5)
        self._draw_hat_indicator((0, 0))  # 初始化为中央位置

        # 用于停止线程的标志
        self.running = True

        # 按键绑定框架
        self.binding_frame = ttk.LabelFrame(self.root, text="按键绑定 / Key binding")
        self.binding_frame.grid(row=6, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.binding_label = ttk.Label(self.binding_frame, text="未进行按键绑定")
        self.binding_label.grid(row=0, column=0, padx=5, pady=5)

        self.binding_button = ttk.Button(
            self.binding_frame,
            text="绑定新按键",
            command=self.start_key_binding
        )
        self.binding_button.grid(row=0, column=1, padx=5, pady=5)

        # 命令绑定区域
        self.command_binding_frame = ttk.LabelFrame(self.root, text="命令绑定 / Command binding")
        self.command_binding_frame.grid(row=7, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.command_label = ttk.Label(self.command_binding_frame, text="未绑定命令")
        self.command_label.grid(row=0, column=0, padx=5, pady=5)

        self.command_entry = ttk.Entry(self.command_binding_frame)
        self.command_entry.grid(row=0, column=1, padx=5, pady=5)

        self.command_bind_button = ttk.Button(
            self.command_binding_frame,
            text="绑定命令",
            command=self.start_command_binding
        )
        self.command_bind_button.grid(row=0, column=2, padx=5, pady=5)

        # 设备 & 轴映射框架 / device & axis mapping
        self._build_device_frame()

        # 按钮框架
        self.button_frame = ttk.Frame(self.root)
        self.button_frame.grid(row=10, column=0, columnspan=4, pady=10)

        # 创建连接按钮
        self.connect_button = ttk.Button(
            self.button_frame,
            text="允许fivem连接",
            command=self.connect_to_fivem
        )
        self.connect_button.grid(row=0, column=0, padx=10)

        # ZR翻转按钮
        self.zr_swap_button = ttk.Button(
            self.button_frame,
            text="ZR翻转: 关闭",
            command=self.toggle_zr_swap
        )
        self.zr_swap_button.grid(row=0, column=1, padx=10)
        self.zr_swapped = False

        # 保存配置按钮
        self.save_config_button = ttk.Button(
            self.button_frame,
            text="保存配置",
            command=self.save_config
        )
        self.save_config_button.grid(row=0, column=2, padx=10)

        # 创建退出按钮
        self.exit_button = ttk.Button(
            self.button_frame,
            text="退出",
            command=self.quit_application
        )
        self.exit_button.grid(row=0, column=3, padx=10)

        # 加载配置 (必须在所有控件创建之后)
        self.load_config()

    def _build_device_frame(self):
        """创建设备信息与轴映射界面 / device info + axis mapping UI."""
        self.device_frame = ttk.LabelFrame(
            self.root, text="设备与轴映射 / Device & Axis Mapping"
        )
        self.device_frame.grid(row=8, column=0, columnspan=4, padx=5, pady=5, sticky="nsew")

        self.device_name_label = ttk.Label(
            self.device_frame, text="摇杆 / Joystick: (未检测 / not detected)"
        )
        self.device_name_label.grid(row=0, column=0, columnspan=4, padx=5, pady=2, sticky="w")

        self.raw_axes_label = ttk.Label(self.device_frame, text="原始轴 / Raw axes: -")
        self.raw_axes_label.grid(row=1, column=0, columnspan=4, padx=5, pady=2, sticky="w")

        # 每个逻辑轴一个下拉框，选择对应的物理轴
        for i, lab in enumerate(AXIS_LABELS):
            ttk.Label(self.device_frame, text=lab).grid(
                row=2 + i, column=0, padx=5, pady=2, sticky="w"
            )
            combo = ttk.Combobox(
                self.device_frame, width=5, state="readonly",
                values=[str(n) for n in range(8)]
            )
            combo.set(str(self.axis_map[i]))
            combo.grid(row=2 + i, column=1, padx=5, pady=2)
            combo.bind("<<ComboboxSelected>>", lambda e, idx=i: self.on_axis_map_change(idx))
            ttk.Label(self.device_frame, text="← 物理轴 / physical axis").grid(
                row=2 + i, column=2, padx=5, pady=2, sticky="w"
            )
            self.axis_combos.append(combo)

    def update_buttons(self, buttons):
        """更新按钮状态，并处理按键绑定"""
        # 先处理键盘按键的释放
        for button_num in self.keyboard_state:
            if str(button_num) not in buttons and self.keyboard_state[button_num]:
                key = self.key_bindings[button_num]
                keyboard.release(key)
                self.keyboard_state[button_num] = False

        # 处理命令绑定
        if self.waiting_for_command and len(buttons) == 1:
            button_num = int(buttons[0])
            self.command_bindings[button_num] = self.temp_command
            self.command_label.config(text=f"命令绑定完成: 按键 {button_num} -> {self.temp_command}")
            self.waiting_for_command = False
            self.temp_command = None
            self.command_bind_button.config(state="normal")
            return

        if buttons:
            self.button_label.config(text=f"按下的按钮: {', '.join(buttons)}")

            # 处理按键绑定
            if self.waiting_for_joystick and len(buttons) == 1:
                self.complete_binding(buttons[0])
                return

            # 模拟按下绑定的键盘按键
            for button in buttons:
                button_num = int(button)
                if button_num in self.key_bindings:
                    key = self.key_bindings[button_num]
                    if not self.keyboard_state.get(button_num, False):
                        keyboard.press(key)
                        self.keyboard_state[button_num] = True

                # 发送绑定的命令
                if button_num in self.command_bindings:
                    command = self.command_bindings[button_num]
                    # 发送命令到WebSocket客户端
                    if self.ws:
                        data = f"cmd:{command}"
                        asyncio.run_coroutine_threadsafe(self.ws.send(data), self.loop)
        else:
            self.button_label.config(text="没有按钮被按下")

    def _draw_hat_indicator(self, hat_value):
        """绘制HAT方向指示器"""
        self.hat_canvas.delete("all")

        # 绘制背景十字
        self.hat_canvas.create_line(50, 10, 50, 90, fill="gray")  # 垂直线
        self.hat_canvas.create_line(10, 50, 90, 50, fill="gray")  # 水平线

        # 根据HAT值确定位置
        x, y = hat_value
        center_x = 50 + (x * 20)  # 20像素的移动范围
        center_y = 50 - (y * 20)  # 20像素的移动范围

        # 绘制当前位置指示器
        self.hat_canvas.create_oval(center_x-5, center_y-5,
                                  center_x+5, center_y+5,
                                  fill="red")

    def update_hat(self, hat_value):
        """更新HAT显示"""
        x, y = hat_value
        # 更新文字显示
        directions = []
        # 修改这两行，交换上下的判断条件
        if y > 0: directions.append("上")
        if y < 0: directions.append("下")
        # 保持左右不变
        if x < 0: directions.append("左")
        if x > 0: directions.append("右")

        direction_text = "中央" if not directions else "、".join(directions)
        self.hat_label.config(text=f"当前方向: {direction_text}")

        # 更新图形显示
        self._draw_hat_indicator(hat_value)

    def display(self , message):
        # 将“检测到摇杆”等消息显示在GUI上
        label = ttk.Label(self.root, text=message)
        label.grid(row=11, column=0, columnspan=3, pady=10)

    # ------------------------------------------------------------------
    # 设备 / 轴映射辅助方法 (从摇杆线程通过 root.after 调用)
    # ------------------------------------------------------------------
    def set_device_name(self, name, numaxes, numbuttons, numhats):
        """显示检测到的摇杆信息 / show detected joystick info."""
        self.current_joystick_name = name
        self.device_name_label.config(
            text=f"摇杆 / Joystick: {name}  |  轴/axes:{numaxes}  键/buttons:{numbuttons}  帽/hats:{numhats}"
        )

    def update_raw_axes(self, raw):
        """实时显示所有物理轴的原始值，方便校准 / live raw axis values."""
        txt = "原始轴 / Raw axes:  " + "   ".join(f"{i}:{v:+.2f}" for i, v in enumerate(raw))
        self.raw_axes_label.config(text=txt)

    def update_combo_ranges(self, count):
        """根据实际轴数量更新下拉框可选范围。"""
        vals = [str(n) for n in range(max(count, 4))]
        for combo in self.axis_combos:
            combo.config(values=vals)

    def apply_axis_map(self, axis_map, reverse=None, axis_count=None):
        """应用一个摇杆配置 (映射 + 反向) 并更新界面。"""
        self.axis_map = list(axis_map)
        if axis_count:
            self.update_combo_ranges(axis_count)
        for i, combo in enumerate(self.axis_combos):
            if i < len(self.axis_map):
                combo.set(str(self.axis_map[i]))
        if reverse is not None:
            self.axis_reversed = list(reverse)
            for i, rev in enumerate(self.axis_reversed):
                self.reverse_buttons[i].config(text="反向" if rev else "正向")

    def on_axis_map_change(self, logical_index):
        """用户手动修改了某个逻辑轴对应的物理轴。"""
        try:
            phys = int(self.axis_combos[logical_index].get())
        except (ValueError, IndexError):
            return
        if logical_index < len(self.axis_map):
            self.axis_map[logical_index] = phys
        self.user_axis_map = True

    def connect_to_fivem(self):
        """连接到FiveM的处理函数"""
        try:
            # 创建新线程运行事件循环
            self.loop_thread = threading.Thread(target=self._run_event_loop)
            self.loop_thread.daemon = True
            self.loop_thread.start()

            # 等待事件循环启动
            time.sleep(0.1)

            # 使用run_coroutine_threadsafe启动WebSocket服务器
            async def start_server():
                self.ws_server = await websockets.serve(
                    lambda ws: self._handle_websocket(ws),  # 使用 lambda 移除 path 参数
                    '127.0.0.1',
                    11556
                )

            future = asyncio.run_coroutine_threadsafe(
                start_server(),
                self.loop
            )
            future.result()  # 等待服务器启动

            # 更新按钮状态
            self.connect_button.config(text="等待连接", state="disabled")
            # self.display("WebSocket服务器已启动,等待客户端连接...")

        except Exception as e:
            print(f"启动失败: {str(e)}")


    async def _handle_websocket(self, websocket):
        """处理WebSocket连接"""
        try:
            self.ws = websocket
            # 更新连接状态显示
            self.root.after(0, lambda: (
                self.connect_button.config(text="已连接", state="disabled"),
                # self.display("客户端已连接")
            ))
            print("客户端已连接")

            # 等待接收连接消息
            message = await websocket.recv()
            print(f"收到消息: {message}")

            if message == 'connect':
                print("开始发送数据")
                # 启动数据发送线程
                self.send_thread = threading.Thread(target=self._send_joystick_data)
                self.send_thread.daemon = True
                self.send_thread.start()

            while self.running:
                await asyncio.sleep(0.1)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"连接关闭原因: {e}")
            # 更新断开连接状态显示
            self.root.after(0, lambda: (
                self.connect_button.config(text="允许fivem连接", state="normal"),
                # self.display("客户端断开连接")
            ))
        except Exception as e:
            print(f"连接错误详情: {str(e)}")
            # 更新错误状态显示
            self.root.after(0, lambda: (
                self.connect_button.config(text="允许fivem连接", state="normal"),
                self.display(f"连接错误: {str(e)}")
            ))
        finally:
            self.ws = None

    def toggle_reverse(self, axis_index):
        """切换指定轴的反向状态"""
        self.axis_reversed[axis_index] = not self.axis_reversed[axis_index]
        button_text = "正向" if not self.axis_reversed[axis_index] else "反向"
        self.reverse_buttons[axis_index].config(text=button_text)

    def update_values(self, values):
        """更新轴的值，考虑反向状态和ZR交换"""
        # 如果启用了ZR交换，交换Z轴和R轴的值
        if self.zr_swapped and len(values) >= 4:
            values[2], values[3] = values[3], values[2]

        for i, value in enumerate(values):
            if i < len(self.sliders):
                actual_value = -value if self.axis_reversed[i] else value
                self.sliders[i].set(actual_value)
                self.labels[i].config(text=f"{actual_value:.2f}")

    def toggle_zr_swap(self):
        """切换Z轴和R轴的交换状态"""
        self.zr_swapped = not self.zr_swapped
        button_text = "ZR翻转: 开启" if self.zr_swapped else "ZR翻转: 关闭"
        self.zr_swap_button.config(text=button_text)



    def _send_joystick_data(self):
        """发送摇杆数据时考虑反向设置和ZR交换"""
        while self.running and self.ws:
            try:
                values = []
                for i, slider in enumerate(self.sliders):
                    # 获取滑块值并应用反向设置
                    value = slider.get()
                    # value = -value if self.axis_reversed[i] else value
                    values.append(value)

                # 如果启用了ZR交换，在发送前交换Z轴和R轴的值
                # if self.zr_swapped and len(values) >= 4:
                #     values[2], values[3] = values[3], values[2]

                # 将数据格式化为字符串
                data = ','.join(f"{v:.3f}" for v in values)
                asyncio.run_coroutine_threadsafe(self.ws.send(data), self.loop)
                time.sleep(1/60)

            except Exception as e:
                print(f"发送数据错误: {str(e)}")
                break

    def _run_event_loop(self):
        """在新线程中运行事件循环"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def quit_application(self):
        """退出应用程序"""
        self.running = False

        # 关闭WebSocket连接
        if self.ws:
            asyncio.run_coroutine_threadsafe(
                self.ws.close(),
                self.loop
            )

        # 停止事件循环
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.loop_thread:
                self.loop_thread.join(timeout=1.0)
            self.loop.close()

        # 退出程序
        self.root.quit()

    def start_key_binding(self):
        """开始按键绑定过程"""
        self.waiting_for_keyboard = True
        self.binding_label.config(text="请按下要绑定的键盘按键...")
        self.binding_button.config(state="disabled")

        # 绑定键盘事件
        self.root.bind('<Key>', self.on_keyboard_press)

    def on_keyboard_press(self, event):
        """处理键盘按键事件"""
        if self.waiting_for_keyboard:
            self.temp_keyboard_key = event.keysym
            self.waiting_for_keyboard = False
            self.waiting_for_joystick = True
            self.binding_label.config(text=f"已记录键盘按键: {event.keysym}\n请按下要绑定的摇杆按键...")
            self.root.unbind('<Key>')

    def complete_binding(self, joystick_button):
        """完成按键绑定"""
        if self.waiting_for_joystick and self.temp_keyboard_key:
            try:
                # 尝试验证按键是否可用
                keyboard.parse_hotkey(self.temp_keyboard_key)

                # 如果验证通过，完成绑定
                self.key_bindings[int(joystick_button)] = self.temp_keyboard_key
                self.binding_label.config(
                    text=f"绑定完成: 摇杆按键 {joystick_button} -> 键盘按键 {self.temp_keyboard_key}"
                )
            except ValueError as e:
                # 按键无效，取消本次绑定
                self.binding_label.config(
                    text=f"绑定失败: 无效的键盘按键 {self.temp_keyboard_key}"
                )
            finally:
                self.waiting_for_joystick = False
                self.temp_keyboard_key = None
                self.binding_button.config(state="normal")

    def start_command_binding(self):
        """开始命令绑定过程"""
        command = self.command_entry.get()
        if not command:
            return

        self.waiting_for_command = True
        self.temp_command = command
        self.command_label.config(text="请按下要绑定的摇杆按键...")
        self.command_bind_button.config(state="disabled")

    def save_config(self):
        """保存配置到文件"""
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
        except Exception as e:
            print(f"保存配置失败: {str(e)}")

    def load_config(self):
        """从文件加载配置"""
        if not os.path.exists('joystick_config.json'):
            return

        try:
            with open('joystick_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 加载轴映射 / axis mapping
            axis_map = config.get('axis_map')
            if axis_map and len(axis_map) == 4:
                self.axis_map = [int(x) for x in axis_map]
                self.user_axis_map = True
                for i, combo in enumerate(self.axis_combos):
                    combo.set(str(self.axis_map[i]))

            # 加载反向设置
            self.axis_reversed = config.get('axis_reversed', [False] * 4)
            for i, reversed_state in enumerate(self.axis_reversed):
                self.reverse_buttons[i].config(text="反向" if reversed_state else "正向")

            # 加载ZR翻转设置
            self.zr_swapped = config.get('zr_swapped', False)
            if self.zr_swapped:
                self.zr_swap_button.config(text="ZR翻转: 开启")

            # 加载按键绑定
            key_bindings = config.get('key_bindings', {})
            self.key_bindings = {int(k): v for k, v in key_bindings.items()}

            # 加载命令绑定
            command_bindings = config.get('command_bindings', {})
            self.command_bindings = {int(k): v for k, v in command_bindings.items()}

        except Exception as e:
            print(f"加载配置失败: {str(e)}")

def joystick_thread(gui):
    pygame.init()
    pygame.joystick.init()

    if pygame.joystick.get_count() == 0:
        print("没有检测到摇杆设备")
        gui.root.after(0, lambda: gui.device_name_label.config(
            text="摇杆 / Joystick: 未检测到 / none detected"
        ))
        return

    joystick = pygame.joystick.Joystick(0)
    joystick.init()

    name = joystick.get_name()
    numaxes = joystick.get_numaxes()
    numbuttons = joystick.get_numbuttons()
    numhats = joystick.get_numhats()

    gui.display("检测到摇杆设备: " + name + "\n" + "按键数: " + str(numbuttons) +
                "\n" + "轴数: " + str(numaxes) + "\n" + "苦力帽数: " + str(numhats))
    gui.root.after(0, gui.set_device_name, name, numaxes, numbuttons, numhats)

    # 自动选择配置 (除非用户已经有自定义/已保存的映射)
    selected = select_profile(name)
    if selected and not gui.user_axis_map:
        key, profile = selected
        print(f"自动应用配置 / applied profile: {key}")
        gui.root.after(0, gui.apply_axis_map,
                       profile["axis_map"], profile["reverse"], numaxes)
    else:
        gui.root.after(0, gui.update_combo_ranges, numaxes)

    try:
        while gui.running:
            pygame.event.pump()

            # 读取HAT状态
            if numhats > 0:
                hat_value = joystick.get_hat(0)  # 获取第一个HAT的值
                gui.root.after(0, gui.update_hat, hat_value)

            # 读取所有物理轴的原始值
            raw = [joystick.get_axis(i) for i in range(numaxes)]
            gui.root.after(0, gui.update_raw_axes, raw)

            # 通过映射把物理轴转换成逻辑轴 X,Y,Z,R
            axes_values = []
            for logical in range(4):
                phys = gui.axis_map[logical] if logical < len(gui.axis_map) else logical
                axes_values.append(raw[phys] if 0 <= phys < len(raw) else 0.0)

            # 读取按钮状态
            pressed_buttons = [str(i) for i in range(numbuttons) if joystick.get_button(i)]

            # 更新GUI
            gui.root.after(0, gui.update_values, axes_values)
            gui.root.after(0, gui.update_buttons, pressed_buttons)

            time.sleep(0.1)

    finally:
        pygame.quit()

if __name__ == "__main__":
    gui = JoystickGUI()

    # 创建并启动摇杆监听线程
    thread = threading.Thread(target=joystick_thread, args=(gui,))
    thread.daemon = True
    thread.start()
    print("摇杆监视器已启动")

    try:
        gui.root.mainloop()
    finally:
        gui.running = False
        # 安全检查 loop_thread 是否存在和是否已初始化
        if hasattr(gui, 'loop_thread') and gui.loop_thread is not None:
            gui.loop_thread.join(timeout=1.0)
        if thread is not None:
            thread.join(timeout=1.0)
