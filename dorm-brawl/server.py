#!/usr/bin/env python3
"""
宿舍大乱斗 — 多人实时对战游戏服务器（支持 AI 机器人）
依赖: pip install aiohttp
运行: python server.py
加入: 浏览器打开 http://<本机IP>:8080
"""
import asyncio
import json
import random
import math
import time
import os
from aiohttp import web, WSMsgType

# ==================== 游戏常量 ====================
ARENA_W, ARENA_H = 800, 600
PLAYER_R = 16
PROJ_R = 7
PLAYER_SPEED = 230        # 像素/秒
PROJ_SPEED = 430
SHOOT_CD = 0.35           # 射击冷却（秒）
MAX_HP = 100
HIT_DMG = 20
PROJ_LIFE = 2.0           # 弹丸存活时间
PWR_INTERVAL = 7.0        # 道具刷新间隔
ROUND_END_DELAY = 4.0     # 回合结束后等待时间
MAX_BOTS = 8              # 机器人数量上限

# 机器人名字池
BOT_NAMES = ['卷王', '熬夜冠军', '游戏宅', '外卖侠', '健身教练', '学霸', '电竞梦', '摸鱼大师']

# 玩家颜色池
COLORS = [
    '#ef4444', '#3b82f6', '#22c55e', '#f59e0b',
    '#a855f7', '#06b6d4', '#ec4899', '#84cc16',
    '#f97316', '#6366f1'
]

# 宿舍家具障碍物 [x, y, w, h]
OBSTACLES = [
    [100, 90, 110, 45],    # 上铺床
    [590, 90, 110, 45],
    [100, 465, 110, 45],   # 下铺床
    [590, 465, 110, 45],
    [340, 260, 120, 70],   # 书桌
]

# 出生点
SPAWNS = [
    (50, 50), (750, 50), (50, 550), (750, 550),
    (400, 50), (400, 550), (50, 300), (750, 300),
    (220, 220), (580, 380),
]


# ==================== 游戏对象 ====================
class Player:
    def __init__(self, pid, name, color):
        self.id = pid
        self.name = name[:12]
        self.color = color
        self.is_bot = False
        self.x = 400
        self.y = 300
        self.vx = 0.0
        self.vy = 0.0
        self.health = MAX_HP
        self.alive = True
        self.score = 0
        self.shoot_cd = 0.0
        # 输入缓存
        self.idx = 0
        self.idy = 0
        self.imx = 400
        self.imy = 300
        self.ishoot = False
        # 道具增益
        self.shield_t = 0.0
        self.speed_t = 0.0
        self.rapid_t = 0.0
        # 击杀者
        self.last_hit_by = None

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'color': self.color,
            'x': round(self.x, 1), 'y': round(self.y, 1),
            'hp': max(0, self.health), 'alive': self.alive,
            'score': self.score, 'bot': self.is_bot,
            'shield': round(self.shield_t, 1),
            'speed': round(self.speed_t, 1),
            'rapid': round(self.rapid_t, 1),
        }


class Projectile:
    def __init__(self, x, y, vx, vy, owner_id, owner_color, owner_name):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.owner = owner_id
        self.color = owner_color
        self.owner_name = owner_name
        self.life = PROJ_LIFE

    def to_dict(self):
        return {
            'x': round(self.x, 1), 'y': round(self.y, 1),
            'color': self.color,
        }


class PowerUp:
    def __init__(self, x, y, kind):
        self.x = x
        self.y = y
        self.kind = kind  # 'speed' / 'shield' / 'rapid'

    def to_dict(self):
        return {'x': round(self.x, 1), 'y': round(self.y, 1), 'kind': self.kind}


# ==================== 全局状态 ====================
players = {}        # pid -> Player
projectiles = []    # [Projectile]
powerups = []       # [PowerUp]
clients = {}        # pid -> WebSocketResponse
bot_ai = {}         # pid -> BotBrain（机器人大脑）
color_idx = 0
player_seq = 0      # 真人玩家 id 序号
bot_seq = 0         # 机器人 id 序号
round_num = 0
round_active = False
round_end_timer = 0.0
powerup_timer = 0.0
kill_feed = []      # [(killer, victim, ts)]


# ==================== 工具函数 ====================
def assign_color():
    global color_idx
    c = COLORS[color_idx % len(COLORS)]
    color_idx += 1
    return c


def resolve_circle_rect(cx, cy, r, rx, ry, rw, rh):
    """圆 vs 矩形碰撞解算：若碰撞则返回修正后的 (cx, cy)，否则返回原值。"""
    closest_x = max(rx, min(cx, rx + rw))
    closest_y = max(ry, min(cy, ry + rh))
    dx = cx - closest_x
    dy = cy - closest_y
    dist = math.hypot(dx, dy)
    if dist < r and dist > 0.001:
        push = r - dist
        cx += dx / dist * push
        cy += dy / dist * push
    elif dist <= 0.001:
        # 圆心在矩形内，推到最近边
        left = cx - rx
        right = rx + rw - cx
        top = cy - ry
        bottom = ry + rh - cy
        m = min(left, right, top, bottom)
        if m == left:
            cx = rx - r
        elif m == right:
            cx = rx + rw + r
        elif m == top:
            cy = ry - r
        else:
            cy = ry + rh + r
    return cx, cy


def spawn_powerup():
    """在随机位置生成道具（避开障碍物）。"""
    for _ in range(20):
        x = random.uniform(40, ARENA_W - 40)
        y = random.uniform(40, ARENA_H - 40)
        ok = True
        for ox, oy, ow, oh in OBSTACLES:
            if ox - 20 <= x <= ox + ow + 20 and oy - 20 <= y <= oy + oh + 20:
                ok = False
                break
        if ok:
            kind = random.choice(['speed', 'shield', 'rapid'])
            powerups.append(PowerUp(x, y, kind))
            return


# ==================== 机器人 AI ====================
def _seg_seg_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
    """两条线段是否相交。"""
    def ccw(ax, ay, bx, by, cx, cy):
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)
    return ccw(x1, y1, x3, y3, x4, y4) != ccw(x2, y2, x3, y3, x4, y4) and \
           ccw(x1, y1, x2, y2, x3, y3) != ccw(x1, y1, x2, y2, x4, y4)


def _seg_rect_intersect(x1, y1, x2, y2, rx, ry, rw, rh):
    """线段是否与矩形相交（含端点在矩形内）。"""
    if (rx <= x1 <= rx + rw and ry <= y1 <= ry + rh) or \
       (rx <= x2 <= rx + rw and ry <= y2 <= ry + rh):
        return True
    for ex1, ey1, ex2, ey2 in ((rx, ry, rx + rw, ry), (rx + rw, ry, rx + rw, ry + rh),
                               (rx + rw, ry + rh, rx, ry + rh), (rx, ry + rh, rx, ry)):
        if _seg_seg_intersect(x1, y1, x2, y2, ex1, ey1, ex2, ey2):
            return True
    return False


def los_clear(x1, y1, x2, y2):
    """两点连线是否没有被障碍物挡住（用于机器人开火判断）。"""
    for ox, oy, ow, oh in OBSTACLES:
        if _seg_rect_intersect(x1, y1, x2, y2, ox, oy, ow, oh):
            return False
    return True


def _point_near_obstacle(x, y, pad):
    """点是否落在障碍物（向外扩 pad）内。"""
    for ox, oy, ow, oh in OBSTACLES:
        if ox - pad <= x <= ox + ow + pad and oy - pad <= y <= oy + oh + pad:
            return True
    return False


def nearest_powerup(bot, max_d):
    """找 max_d 范围内最近的道具。"""
    best_pu = None
    best = max_d
    for pu in powerups:
        d = math.hypot(pu.x - bot.x, pu.y - bot.y)
        if d < best:
            best = d
            best_pu = pu
    return best_pu


class BotBrain:
    """机器人大脑：保存走位、瞄准、开火节奏等决策状态。"""

    __slots__ = ('strafe', 'strafe_t', 'think_t', 'react_t', 'fire_t',
                 'pause_t', 'wander', 'err_x', 'err_y', 'last_tgt')

    def __init__(self):
        self.strafe = random.choice((-1, 1))  # 绕目标转的方向
        self.strafe_t = 0.0                   # 多久后换向
        self.think_t = 0.0                    # 下次“思考”倒计时
        self.react_t = 0.5                    # 反应时间（看到新目标后延迟开火）
        self.fire_t = 0.0                     # 点射：开火时长
        self.pause_t = random.uniform(0.2, 0.6)  # 点射：停火时长
        self.wander = None                    # 闲逛目标点
        self.err_x = 0.0                      # 瞄准误差
        self.err_y = 0.0
        self.last_tgt = None                  # 上一个目标 id（用于反应时间）


def bot_update(bot, brain, dt):
    """每帧驱动一个机器人：输出和人类玩家一样的输入（idx/idy/imx/imy/ishoot）。"""
    if not round_active or not bot.alive:
        bot.idx = 0
        bot.idy = 0
        bot.ishoot = False
        return

    brain.think_t -= dt
    brain.react_t -= dt
    brain.strafe_t -= dt

    # 找最近的活着的敌人
    target = None
    target_dist = 1e9
    for p in players.values():
        if p.alive and p.id != bot.id:
            d = math.hypot(p.x - bot.x, p.y - bot.y)
            if d < target_dist:
                target_dist = d
                target = p

    # 换了新目标要反应一下才开枪
    tgt_id = target.id if target else None
    if tgt_id != brain.last_tgt:
        brain.last_tgt = tgt_id
        if tgt_id is not None:
            brain.react_t = max(brain.react_t, random.uniform(0.15, 0.35))

    # 低频“思考”：换走位方向、重掷瞄准误差
    if brain.think_t <= 0:
        brain.think_t = 0.12
        if brain.strafe_t <= 0:
            brain.strafe = -brain.strafe
            brain.strafe_t = random.uniform(0.7, 1.8)
        brain.err_x = random.uniform(-26, 26)
        brain.err_y = random.uniform(-26, 26)

    # ---- 移动：保持中距离 + 横向绕圈 + 捡道具 ----
    mx = my = 0.0
    if target is not None:
        nx = (target.x - bot.x) / target_dist
        ny = (target.y - bot.y) / target_dist
        if target_dist > 280:
            ax, ay = nx, ny          # 太远 → 追
        elif target_dist < 170:
            ax, ay = -nx, -ny        # 太近 → 拉开
        else:
            ax = ay = 0.0            # 距离合适 → 不进退
        mx = ax - ny * brain.strafe * 0.85
        my = ay + nx * brain.strafe * 0.85
        # 附近有道具且敌人还远 → 去捡
        pu = nearest_powerup(bot, 200)
        if pu is not None and target_dist > 260:
            pdx = pu.x - bot.x
            pdy = pu.y - bot.y
            pd = math.hypot(pdx, pdy) or 1
            mx, my = pdx / pd, pdy / pd
    else:
        # 没有敌人 → 闲逛
        if brain.wander is None or \
           math.hypot(brain.wander[0] - bot.x, brain.wander[1] - bot.y) < 30:
            brain.wander = (random.uniform(60, ARENA_W - 60),
                            random.uniform(60, ARENA_H - 60))
        dx = brain.wander[0] - bot.x
        dy = brain.wander[1] - bot.y
        d = math.hypot(dx, dy) or 1
        mx, my = dx / d, dy / d

    # 归一化 + 避障：看前方一小段，被挡住就逐步偏转方向
    d = math.hypot(mx, my)
    if d > 0.01:
        mx /= d
        my /= d
        for ang in (0, 0.6, -0.6, 1.2, -1.2, 1.9, -1.9, 2.6, -2.6, math.pi):
            ca = math.cos(ang)
            sa = math.sin(ang)
            rx = mx * ca - my * sa
            ry = mx * sa + my * ca
            tx = bot.x + rx * 46
            ty = bot.y + ry * 46
            if PLAYER_R < tx < ARENA_W - PLAYER_R and \
               PLAYER_R < ty < ARENA_H - PLAYER_R and \
               not _point_near_obstacle(tx, ty, PLAYER_R):
                mx, my = rx, ry
                break
    bot.idx = mx
    bot.idy = my

    # ---- 瞄准 + 射击 ----
    if target is not None:
        # 按弹速预判提前量，再叠加瞄准误差
        tof = target_dist / PROJ_SPEED
        bot.imx = target.x + target.vx * tof * 0.85 + brain.err_x
        bot.imy = target.y + target.vy * tof * 0.85 + brain.err_y
        # 点射节奏：打一阵、停一下
        if brain.fire_t > 0:
            brain.fire_t -= dt
            want_fire = True
        elif brain.pause_t > 0:
            brain.pause_t -= dt
            want_fire = False
        else:
            brain.fire_t = random.uniform(0.5, 1.1)
            brain.pause_t = random.uniform(0.25, 0.6)
            want_fire = True
        bot.ishoot = want_fire and brain.react_t <= 0 and target_dist < 560 \
            and los_clear(bot.x, bot.y, target.x, target.y)
    else:
        bot.ishoot = False
        bot.imx = bot.x + mx * 50
        bot.imy = bot.y + my * 50


def spawn_bot():
    """生成一个机器人并放进 players。"""
    global bot_seq
    bot_seq += 1
    pid = 'b%d' % bot_seq
    name = '机器人·%s' % BOT_NAMES[(bot_seq - 1) % len(BOT_NAMES)]
    bot = Player(pid, name, assign_color())
    bot.is_bot = True
    # 随机出生点（避开障碍物）
    for _ in range(30):
        x = random.uniform(50, ARENA_W - 50)
        y = random.uniform(50, ARENA_H - 50)
        if not _point_near_obstacle(x, y, PLAYER_R + 10):
            bot.x, bot.y = x, y
            break
    players[pid] = bot
    bot_ai[pid] = BotBrain()
    return bot


def ensure_bots(n):
    """保证房间里至少有 n 个机器人（不超过人数上限），返回新增数量。"""
    n = max(0, min(MAX_BOTS, int(n)))
    added = 0
    while sum(1 for p in players.values() if p.is_bot) < n and len(players) < 10:
        spawn_bot()
        added += 1
    return added


def set_bots(n):
    """把机器人数量精确调整为 n：不够就补，多了就删。返回 (新增数, 删除数)。"""
    global round_active
    n = max(0, min(MAX_BOTS, int(n)))
    bot_ids = [pid for pid, p in players.items() if p.is_bot]
    added = removed = 0
    # 补齐
    while len(bot_ids) < n and len(players) < 10:
        bot_ids.append(spawn_bot().id)
        added += 1
    # 多删（优先删已阵亡的，避免打断观战体验）
    while len(bot_ids) > n:
        victim = next((pid for pid in bot_ids if not players[pid].alive), bot_ids[-1])
        bot_ids.remove(victim)
        del players[victim]
        bot_ai.pop(victim, None)
        removed += 1
    # 删完人不够两个了 → 终止当前回合，等人来再开
    if removed and len(players) < 2:
        round_active = False
    return added, removed


def remove_all_bots():
    """移除所有机器人（没有真人玩家时调用），并重置房间状态。"""
    global round_active, round_end_timer
    for pid in [pid for pid, p in players.items() if p.is_bot]:
        del players[pid]
        bot_ai.pop(pid, None)
    round_active = False
    round_end_timer = 0.0
    projectiles.clear()
    powerups.clear()


def start_round():
    """开始新回合：所有玩家复活并随机出生。"""
    global round_active, round_end_timer, powerup_timer, round_num
    round_num += 1
    round_active = True
    round_end_timer = 0.0
    # 清空弹丸和道具
    projectiles.clear()
    powerups.clear()
    # 出生点洗牌
    pts = SPAWNS[:]
    random.shuffle(pts)
    for i, p in enumerate(players.values()):
        if i < len(pts):
            p.x, p.y = pts[i]
        else:
            p.x = random.uniform(40, ARENA_W - 40)
            p.y = random.uniform(40, ARENA_H - 40)
        p.vx = 0
        p.vy = 0
        p.health = MAX_HP
        p.alive = True
        p.shoot_cd = 0
        p.shield_t = 0
        p.speed_t = 0
        p.rapid_t = 0
    # 机器人重新进入战斗状态（给玩家一点反应时间）
    for brain in bot_ai.values():
        brain.react_t = random.uniform(0.5, 1.0)
        brain.last_tgt = None
        brain.wander = None
    powerup_timer = 3.0  # 3 秒后开始刷道具


# ==================== 游戏更新逻辑 ====================
def update(dt):
    global round_end_timer, powerup_timer, round_active

    # --- 机器人 AI：先算出它们的输入，后面走和人类一样的物理 ---
    for p in players.values():
        if p.is_bot and p.alive:
            brain = bot_ai.get(p.id)
            if brain is None:
                brain = bot_ai[p.id] = BotBrain()
            bot_update(p, brain, dt)

    # --- 更新玩家 ---
    for p in players.values():
        if not p.alive:
            continue

        # 道具计时
        p.shield_t = max(0, p.shield_t - dt)
        p.speed_t = max(0, p.speed_t - dt)
        p.rapid_t = max(0, p.rapid_t - dt)

        # 输入 → 加速度
        accel = 1400
        friction = 7.5
        cap = PLAYER_SPEED * (1.6 if p.speed_t > 0 else 1)

        p.vx += p.idx * accel * dt
        p.vy += p.idy * accel * dt
        # 摩擦
        f = math.exp(-friction * dt)
        p.vx *= f
        p.vy *= f
        # 限速
        sp = math.hypot(p.vx, p.vy)
        if sp > cap:
            p.vx = p.vx / sp * cap
            p.vy = p.vy / sp * cap

        # 移动
        p.x += p.vx * dt
        p.y += p.vy * dt

        # 墙壁碰撞
        p.x = max(PLAYER_R, min(ARENA_W - PLAYER_R, p.x))
        p.y = max(PLAYER_R, min(ARENA_H - PLAYER_R, p.y))

        # 障碍物碰撞
        for ox, oy, ow, oh in OBSTACLES:
            p.x, p.y = resolve_circle_rect(p.x, p.y, PLAYER_R, ox, oy, ow, oh)

        # 射击
        p.shoot_cd -= dt
        if p.ishoot and p.shoot_cd <= 0:
            cd = SHOOT_CD * (0.35 if p.rapid_t > 0 else 1)
            p.shoot_cd = cd
            dx = p.imx - p.x
            dy = p.imy - p.y
            d = math.hypot(dx, dy) or 1
            dx /= d
            dy /= d
            projectiles.append(Projectile(
                p.x + dx * (PLAYER_R + 3),
                p.y + dy * (PLAYER_R + 3),
                dx * PROJ_SPEED,
                dy * PROJ_SPEED,
                p.id, p.color, p.name
            ))

    # --- 更新弹丸 ---
    for proj in projectiles[:]:
        proj.x += proj.vx * dt
        proj.y += proj.vy * dt
        proj.life -= dt

        # 出界
        if proj.x < 0 or proj.x > ARENA_W or proj.y < 0 or proj.y > ARENA_H:
            projectiles.remove(proj)
            continue

        # 撞障碍物
        hit_obstacle = False
        for ox, oy, ow, oh in OBSTACLES:
            if ox <= proj.x <= ox + ow and oy <= proj.y <= oy + oh:
                hit_obstacle = True
                break
        if hit_obstacle:
            projectiles.remove(proj)
            continue

        # 撞玩家
        hit_player = False
        for p in players.values():
            if p.alive and p.id != proj.owner:
                if math.hypot(p.x - proj.x, p.y - proj.y) < PLAYER_R + PROJ_R:
                    if p.shield_t <= 0:
                        p.health -= HIT_DMG
                        p.last_hit_by = proj.owner
                        if p.health <= 0:
                            p.alive = False
                            killer = players.get(proj.owner)
                            kname = killer.name if killer else '未知'
                            kill_feed.append((kname, p.name, time.time()))
                            if killer:
                                killer.score += 1
                    hit_player = True
                    break
        if hit_player:
            projectiles.remove(proj)
            continue

        # 过期
        if proj.life <= 0:
            if proj in projectiles:
                projectiles.remove(proj)

    # --- 道具 ---
    powerup_timer -= dt
    if powerup_timer <= 0 and len(powerups) < 3:
        spawn_powerup()
        powerup_timer = PWR_INTERVAL

    # 拾取道具
    for pu in powerups[:]:
        for p in players.values():
            if p.alive and math.hypot(p.x - pu.x, p.y - pu.y) < PLAYER_R + 14:
                if pu.kind == 'speed':
                    p.speed_t = 5.0
                elif pu.kind == 'shield':
                    p.shield_t = 4.0
                elif pu.kind == 'rapid':
                    p.rapid_t = 5.0
                if pu in powerups:
                    powerups.remove(pu)
                break

    # --- 回合判定 ---
    alive_count = sum(1 for p in players.values() if p.alive)
    total_count = len(players)

    if round_active and total_count >= 2 and alive_count <= 1:
        round_active = False
        round_end_timer = ROUND_END_DELAY

    if round_end_timer > 0:
        round_end_timer -= dt
        if round_end_timer <= 0 and total_count >= 2:
            start_round()

    # 清理过期击杀提示
    now = time.time()
    kill_feed[:] = [k for k in kill_feed if now - k[2] < 5]


# ==================== 状态序列化 ====================
def get_state():
    return {
        't': 'state',
        'players': [p.to_dict() for p in players.values()],
        'projectiles': [pr.to_dict() for pr in projectiles],
        'powerups': [pu.to_dict() for pu in powerups],
        'obstacles': OBSTACLES,
        'round': round_num,
        'roundActive': round_active,
        'alive': sum(1 for p in players.values() if p.alive),
        'total': len(players),
        'killfeed': [{'killer': k, 'victim': v} for k, v, _ in kill_feed[-5:]],
        'winner': next((p.name for p in players.values() if p.alive), None) if not round_active and round_end_timer > 0 else None,
    }


# ==================== WebSocket 处理 ====================
async def ws_handler(request):
    global player_seq
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    player_seq += 1
    pid = 'p%d' % player_seq
    name = '玩家%d' % player_seq
    color = assign_color()
    player = Player(pid, name, color)
    players[pid] = player
    clients[pid] = ws

    # 确认加入
    await ws.send_json({'t': 'joined', 'id': pid, 'color': color, 'name': name,
                         'arena': [ARENA_W, ARENA_H]})

    # 如果人数 >= 2 且当前无回合，自动开始
    if len(players) >= 2 and not round_active and round_end_timer <= 0:
        start_round()

    # 广播玩家加入
    for cid, c in clients.items():
        if cid != pid and not c.closed:
            try:
                await c.send_json({'t': 'info', 'msg': '%s 加入了房间' % name})
            except Exception:
                pass

    # 处理消息
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    mtype = data.get('t')

                    if mtype == 'join' and 'name' in data:
                        player.name = data['name'][:12] or name
                        await ws.send_json({'t': 'nameok', 'name': player.name})
                        # 按需添加机器人：保证房间里至少有 bots 个
                        try:
                            n_bots = int(data.get('bots', 0))
                        except (TypeError, ValueError):
                            n_bots = 0
                        if n_bots > 0:
                            added = ensure_bots(n_bots)
                            if len(players) >= 2 and not round_active and round_end_timer <= 0:
                                start_round()
                            if added > 0:
                                for cid, c in clients.items():
                                    if not c.closed:
                                        try:
                                            await c.send_json({'t': 'info',
                                                               'msg': '%d 个机器人加入了战场' % added})
                                        except Exception:
                                            pass

                    elif mtype == 'input':
                        if player.alive:
                            player.idx = max(-1, min(1, float(data.get('dx', 0))))
                            player.idy = max(-1, min(1, float(data.get('dy', 0))))
                            player.imx = max(0, min(ARENA_W, float(data.get('mx', 400))))
                            player.imy = max(0, min(ARENA_H, float(data.get('my', 300))))
                            player.ishoot = bool(data.get('shoot', False))

                    elif mtype == 'chat' and 'msg' in data:
                        text = str(data['msg'])[:80]
                        for cid, c in clients.items():
                            if not c.closed:
                                try:
                                    await c.send_json({'t': 'chat', 'name': player.name,
                                                       'color': player.color, 'msg': text})
                                except Exception:
                                    pass

                    elif mtype == 'bots':
                        # 游戏中随时调整机器人数量
                        try:
                            n_bots = int(data.get('n', 0))
                        except (TypeError, ValueError):
                            continue
                        added, removed = set_bots(n_bots)
                        if added and len(players) >= 2 and not round_active \
                                and round_end_timer <= 0:
                            start_round()
                        if added or removed:
                            text = '%d 个机器人加入了战场' % added if added \
                                else '%d 个机器人被移出战场' % removed
                            for cid, c in clients.items():
                                if not c.closed:
                                    try:
                                        await c.send_json({'t': 'info', 'msg': text})
                                    except Exception:
                                        pass

                except (json.JSONDecodeError, ValueError, KeyError):
                    pass

            elif msg.type == WSMsgType.ERROR:
                break
    except Exception:
        pass
    finally:
        # 清理离线玩家
        if pid in players:
            del players[pid]
        if pid in clients:
            del clients[pid]
        # 房间里没有真人了 → 清掉所有机器人并重置房间
        if not any(not p.is_bot for p in players.values()):
            remove_all_bots()
        for cid, c in clients.items():
            if not c.closed:
                try:
                    await c.send_json({'t': 'info', 'msg': '%s 离开了房间' % name})
                except Exception:
                    pass

    return ws


# ==================== HTTP 页面 ====================
async def index_handler(request):
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text='<h1>index.html not found</h1><p>请确保 index.html 与 server.py 在同一目录</p>',
                           content_type='text/html')


# ==================== 游戏循环 ====================
async def game_loop():
    last = time.time()
    while True:
        now = time.time()
        dt = min(now - last, 0.05)
        last = now

        if players:
            update(dt)

        # 广播状态
        state = get_state()
        state_json = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
        for cid in list(clients.keys()):
            c = clients.get(cid)
            if c and not c.closed:
                try:
                    await c.send_str(state_json)
                except Exception:
                    pass

        await asyncio.sleep(1 / 30)


# ==================== 主入口 ====================
async def on_startup(app):
    """服务器启动时开启游戏循环"""
    asyncio.ensure_future(game_loop())


def main():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/ws', ws_handler)
    app.on_startup.append(on_startup)

    port = int(os.environ.get('PORT', 8080))
    print()
    print('=' * 50)
    print('  宿舍大乱斗 — 游戏服务器已启动')
    print('  端口: %d' % port)
    print('  局域网加入地址:')

    # 尝试打印本机 IP
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
        print('  http://%s:%d' % (local_ip, port))
    except Exception:
        print('  http://<本机IP>:%d' % port)

    print('  本机: http://localhost:%d' % port)
    print('=' * 50)
    print()

    web.run_app(app, host='0.0.0.0', port=port, print=None)


if __name__ == '__main__':
    main()
