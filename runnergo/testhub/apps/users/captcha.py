"""
图形验证码模块
生成验证码图片并存储到 Redis，用于短信发送前的人机校验
"""
import io
import random
import string
import uuid
import base64
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from .redis_client import get_redis


def generate_captcha_code(length=4):
    """生成随机验证码字符串（字母+数字）"""
    chars = string.ascii_uppercase + string.digits
    # 剔除容易混淆的字符
    chars = chars.replace('O', '').replace('0', '').replace('I', '').replace('1', '').replace('L', '')
    return ''.join(random.choices(chars, k=length))


def create_captcha_image(code):
    """根据验证码字符串生成图片，返回 base64 编码"""
    width, height = 120, 44
    image = Image.new('RGB', (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(image)

    # 背景噪点
    for _ in range(random.randint(60, 100)):
        x = random.randint(0, width)
        y = random.randint(0, height)
        draw.point((x, y), fill=(random.randint(160, 220), random.randint(160, 220), random.randint(160, 220)))

    # 绘制字符
    try:
        # 尝试使用系统自带字体
        font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 28)
    except (IOError, OSError):
        try:
            font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 28)
        except (IOError, OSError):
            font = ImageFont.load_default()

    for i, char in enumerate(code):
        x = 10 + i * 26 + random.randint(-3, 3)
        y = random.randint(2, 10)
        # 随机颜色
        color = (random.randint(0, 100), random.randint(0, 100), random.randint(150, 255))
        draw.text((x, y), char, font=font, fill=color)

    # 干扰线
    for _ in range(random.randint(2, 4)):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        draw.line([(x1, y1), (x2, y2)], fill=(random.randint(100, 180), random.randint(100, 180), random.randint(100, 180)), width=1)

    # 轻微模糊
    image = image.filter(ImageFilter.GaussianBlur(radius=0.3))

    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('utf-8')


def create_captcha(ttl=300):
    """
    生成验证码并存储到 Redis
    返回 {'token': str, 'image': str (base64)}
    """
    code = generate_captcha_code()
    token = str(uuid.uuid4())
    image = create_captcha_image(code)

    redis_client = get_redis()
    redis_client.setex(f"captcha:{token}", ttl, code)

    return {'token': token, 'image': image}


def validate_captcha(token, code):
    """校验图形验证码，校验成功后删除"""
    redis_client = get_redis()
    key = f"captcha:{token}"
    stored_code = redis_client.get(key)
    if stored_code is None:
        return False
    stored_code = stored_code.decode('utf-8') if isinstance(stored_code, bytes) else stored_code
    if stored_code.upper() == code.upper():
        redis_client.delete(key)
        return True
    return False
