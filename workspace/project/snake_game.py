"""
貪吃蛇遊戲 - 月月見特製 Turtle 版 (=^･ω･^=)
使用方向鍵控制蛇蛇移動，吃到紅色果果就會長大！
純 Python 內建 turtle 模組，無需安裝任何東西喵～
"""

import turtle
import random
import time
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ========== 遊戲設定 ==========
WINDOW_SIZE = 600
CELL_SIZE = 20
GRID_SIZE = WINDOW_SIZE // CELL_SIZE  # 30x30
DELAY = 0.1  # 遊戲速度（秒）

# ========== 初始化畫面 ==========
wn = turtle.Screen()
wn.title("🐍 貪吃蛇 - 月月見特製版 (=^･ω･^=)")
wn.setup(WINDOW_SIZE + 40, WINDOW_SIZE + 40)
wn.bgcolor("#1e1e28")
wn.tracer(0)  # 手動更新畫面

# ========== 蛇頭 ==========
head = turtle.Turtle()
head.shape("square")
head.color("#64dc64")
head.penup()
head.speed(0)
head.goto(0, 0)
head.direction = "Right"

# ========== 食物 ==========
food = turtle.Turtle()
food.shape("circle")
food.color("#ff5050")
food.penup()
food.speed(0)

# 食物發光圈
food_glow = turtle.Turtle()
food_glow.shape("circle")
food_glow.shapesize(stretch_wid=1.5, stretch_len=1.5)
food_glow.color("#ff6666")
food_glow.penup()
food_glow.speed(0)
food_glow.hideturtle()

# ========== 分數顯示 ==========
score_display = turtle.Turtle()
score_display.color("#ffd700")
score_display.penup()
score_display.speed(0)
score_display.hideturtle()
score_display.goto(-WINDOW_SIZE//2 + 10, WINDOW_SIZE//2 - 30)
score_display.write("🍎 分數: 0  |  🐍 長度: 3", font=("Arial", 14, "bold"))

# 遊戲結束文字
gameover_display = turtle.Turtle()
gameover_display.color("#ff5050")
gameover_display.penup()
gameover_display.speed(0)
gameover_display.hideturtle()

# ========== 遊戲狀態 ==========
snake_body = []  # 儲存身體的 turtle 物件
score = 0
game_over = False
paused = False


# ========== 移動控制 ==========
def go_up():
    if head.direction != "Down":
        head.direction = "Up"

def go_down():
    if head.direction != "Up":
        head.direction = "Down"

def go_left():
    if head.direction != "Right":
        head.direction = "Left"

def go_right():
    if head.direction != "Left":
        head.direction = "Right"

def toggle_pause():
    global paused
    paused = not paused

def restart():
    global game_over, score, paused
    # 清除舊身體
    for seg in snake_body:
        seg.hideturtle()
        seg.clear()
    snake_body.clear()

    # 重置蛇頭
    head.goto(0, 0)
    head.direction = "Right"
    head.showturtle()

    # 重置分數
    score = 0
    game_over = False
    paused = False
    gameover_display.clear()
    update_score_display()
    spawn_food()
    # 重建初始身體
    for _ in range(2):
        add_segment()


def move():
    """根據方向移動蛇頭"""
    x, y = head.position()
    if head.direction == "Up":
        head.sety(y + CELL_SIZE)
    elif head.direction == "Down":
        head.sety(y - CELL_SIZE)
    elif head.direction == "Left":
        head.setx(x - CELL_SIZE)
    elif head.direction == "Right":
        head.setx(x + CELL_SIZE)


def spawn_food():
    """隨機生成食物，避開蛇身"""
    occupied = [(head.xcor(), head.ycor())]
    for seg in snake_body:
        occupied.append((seg.xcor(), seg.ycor()))

    available = []
    half = GRID_SIZE // 2
    for gx in range(-half, half):
        for gy in range(-half, half):
            pos = (gx * CELL_SIZE, gy * CELL_SIZE)
            if pos not in occupied:
                available.append(pos)

    if available:
        fx, fy = random.choice(available)
        food.goto(fx, fy)
        food_glow.goto(fx, fy)
        food.showturtle()
        food_glow.showturtle()
    else:
        food.hideturtle()
        food_glow.hideturtle()


def add_segment():
    """在蛇尾新增一節身體"""
    seg = turtle.Turtle()
    seg.shape("square")
    seg.color("#3cb43c")
    seg.penup()
    seg.speed(0)
    if snake_body:
        seg.goto(snake_body[-1].position())
    else:
        seg.goto(head.position())
    snake_body.append(seg)


def move_body():
    """身體跟隨蛇頭移動"""
    if not snake_body:
        return
    # 從尾部往前更新位置
    for i in range(len(snake_body) - 1, 0, -1):
        prev = snake_body[i - 1].position()
        snake_body[i].goto(prev)
    snake_body[0].goto(head.position())


def check_collision():
    """檢查蛇頭是否撞牆或撞自己"""
    x, y = head.position()
    half = GRID_SIZE // 2 * CELL_SIZE

    # 撞牆
    if abs(x) > half - CELL_SIZE//2 or abs(y) > half - CELL_SIZE//2:
        return True

    # 撞自己
    for seg in snake_body:
        if head.distance(seg) < 5:
            return True

    return False


def update_score_display():
    score_display.clear()
    score_display.write(
        f"🍎 分數: {score}  |  🐍 長度: {len(snake_body) + 1}",
        font=("Arial", 14, "bold"),
    )


def show_gameover():
    global game_over
    game_over = True
    gameover_display.goto(0, 40)
    gameover_display.write(
        "GAME OVER 喵~",
        align="center",
        font=("Arial", 30, "bold"),
    )
    gameover_display.goto(0, -10)
    gameover_display.write(
        f"最終分數: {score}  |  蛇蛇長度: {len(snake_body) + 1}",
        align="center",
        font=("Arial", 16, "normal"),
    )
    gameover_display.goto(0, -50)
    gameover_display.write(
        "按 R 重新開始 | 按 ESC 離開",
        align="center",
        font=("Arial", 14, "normal"),
    )


def game_loop():
    """主遊戲迴圈"""
    global score, game_over

    if not game_over and not paused:
        # 先移動（舊頭位置給身體第一節）
        prev_head_pos = head.position()
        move()

        # 檢查碰撞
        if check_collision():
            show_gameover()
        else:
            # 移動身體
            move_body()

            # 檢查吃到食物
            if head.distance(food) < 15:
                score += 10
                add_segment()
                spawn_food()
                update_score_display()

    wn.update()
    wn.ontimer(game_loop, int(DELAY * 1000))


# ========== 鍵盤綁定 ==========
wn.listen()
wn.onkeypress(go_up, "Up")
wn.onkeypress(go_down, "Down")
wn.onkeypress(go_left, "Left")
wn.onkeypress(go_right, "Right")
wn.onkeypress(toggle_pause, "p")
wn.onkeypress(toggle_pause, "space")
wn.onkeypress(restart, "r")
wn.onkeypress(wn.bye, "Escape")

# ========== 初始設定 ==========
# 畫邊框
border = turtle.Turtle()
border.color("#3c3c4b")
border.pensize(3)
border.penup()
border.speed(0)
border.hideturtle()
half = GRID_SIZE // 2 * CELL_SIZE
border.goto(-half, -half)
border.pendown()
for _ in range(4):
    border.forward(half * 2)
    border.left(90)

# 初始身體
for _ in range(2):
    add_segment()

spawn_food()
update_score_display()

# 啟動遊戲！
print("=" * 50)
print("[Snake] Snake Game - 月月見特製版 啟動喵! (=^w^=)")
print("   方向鍵：控制蛇蛇移動")
print("   空白鍵 / P：暫停")
print("   R：重新開始")
print("   ESC：離開遊戲")
print("=" * 50)

game_loop()
wn.mainloop()
