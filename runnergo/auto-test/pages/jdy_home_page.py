import allure
from playwright.sync_api import Page
from pages.base_page import BasePage


class JdyHomePage(BasePage):
    def __init__(self, page: Page):
        super().__init__(page)

    @allure.step("打开首页")
    def goto(self, channel_id="JDYFWH", product_id="JDYPRD01"):
        self.page.goto(
            f"{self.base_url}/home?channelId={channel_id}&productId={product_id}",
            timeout=30000
        )
        self.page.wait_for_load_state("networkidle")
        self.page.evaluate("localStorage.clear()")
        self.page.wait_for_selector('input[name="phone"]', timeout=10000)

    @allure.step("输入手机号")
    def input_phone(self, phone: str):
        self.page.locator('input[name="phone"]').fill(phone)

    @allure.step("点击发送验证码")
    def click_send_captcha(self):
        self.page.locator('button.form-container-send-btn').click()
        self.page.wait_for_timeout(300)

    @allure.step("输入验证码")
    def input_captcha(self, captcha: str):
        self.page.locator('input[name="password"]').fill(captcha)

    @allure.step("输入车牌号: {plate}")
    def input_car_number(self, plate: str) -> str:
        """通过虚拟键盘输入车牌号：移动端H5需要用touchstart/touchend事件模拟点击"""
        chars = list(plate)
        if not chars:
            return ""

        # 点击第1个格子触发虚拟键盘弹出
        self.page.locator('.car-input-item').first.click(force=True)
        self.page.wait_for_timeout(500)

        # 确认虚拟键盘已弹出
        keyboard = self.page.locator('.car-keyboard')
        if keyboard.count() == 0:
            allure.attach("虚拟键盘未弹出", name="车牌号输入失败")
            return self.get_car_number_text()

        # 第1个字符是省份简称，在中文模式下直接输入
        missing = []
        if not self._tap_car_key(chars[0]):
            missing.append(chars[0])
        self.page.wait_for_timeout(150)

        # 切换到英文/数字模式
        self._switch_car_keyboard_to_en()
        self.page.wait_for_timeout(200)

        # 输入剩余字符
        for char in chars[1:]:
            if not self._tap_car_key(char):
                missing.append(char)
            self.page.wait_for_timeout(120)

        # 点击确认按钮（用touch事件）
        self._tap_element('.car-tooltips-submit')
        self.page.wait_for_timeout(200)

        # Vue 兼容方式关闭键盘弹窗：点击 overlay 触发 Vue 的关闭回调
        # 绝不使用 style.display='none'（会破坏 Vue 内部状态导致后续组件失效）
        self._close_car_keyboard_vue_safe()
        entered = self.get_car_number_text()
        if missing:
            allure.attach(
                f"目标车牌: {plate}\n页面实际: {entered}\n键盘未提供字符: {''.join(missing)}",
                name="车牌号输入校验"
            )
        return entered

    def _close_car_keyboard_vue_safe(self):
        """Vue 兼容方式关闭车牌号键盘弹窗。
        点击 overlay 触发 Vue 的关闭回调，绝不强制 display:none。"""
        for _ in range(3):
            overlay = self.page.locator('.van-overlay:visible')
            popup = self.page.locator('.van-popup:visible')
            if overlay.count() == 0 and popup.count() == 0:
                return
            # 点击 overlay 触发 Vue close
            if overlay.count() > 0:
                try:
                    overlay.first.click(force=True)
                    self.page.wait_for_timeout(150)
                except Exception:
                    pass
            # 如果键盘 popup 还在，尝试按提交/取消按钮
            for sel in ['.car-tooltips-submit', '.van-picker__cancel']:
                try:
                    btn = self.page.locator(sel)
                    if btn.is_visible(timeout=500):
                        btn.click(force=True)
                        self.page.wait_for_timeout(150)
                except Exception:
                    pass
            self.page.wait_for_timeout(100)

    def _tap_car_key(self, char: str) -> bool:
        """用touchstart/touchend事件模拟点击虚拟键盘按键（移动端H5绑定的是touch事件）"""
        return self.page.evaluate("""(char) => {
            const btns = document.querySelectorAll('.car-keyboard-grids-btn');
            for (const btn of btns) {
                if (btn.textContent.trim() === char && btn.offsetParent !== null) {
                    btn.dispatchEvent(new TouchEvent('touchstart', {bubbles: true}));
                    btn.dispatchEvent(new TouchEvent('touchend', {bubbles: true}));
                    return true;
                }
            }
            return false;
        }""", char)

    def _tap_element(self, selector: str):
        """用touchstart/touchend事件模拟点击指定元素"""
        self.page.evaluate(f"""() => {{
            const el = document.querySelector('{selector}');
            if (el) {{
                el.dispatchEvent(new TouchEvent('touchstart', {{bubbles: true}}));
                el.dispatchEvent(new TouchEvent('touchend', {{bubbles: true}}));
                return true;
            }}
            return false;
        }}""")

    def _switch_car_keyboard_to_en(self):
        """切换虚拟键盘到英文/数字模式"""
        self.page.evaluate("""() => {
            const changeBtn = document.querySelector('.car-keyboard-change');
            if (changeBtn) {
                const zhSpan = changeBtn.querySelector('.zh');
                if (zhSpan && zhSpan.classList.contains('active')) {
                    changeBtn.dispatchEvent(new TouchEvent('touchstart', {bubbles: true}));
                    changeBtn.dispatchEvent(new TouchEvent('touchend', {bubbles: true}));
                }
            }
        }""")
        self.page.wait_for_timeout(200)

    @allure.step("勾选同意协议")
    def check_agree(self):
        checkbox = self.page.locator('.van-checkbox.read-agree-box')
        if checkbox.get_attribute('aria-checked') == 'false':
            checkbox.click()

    @allure.step("点击同意并申请")
    def click_submit(self):
        # 首页提交按钮使用 Playwright click（该页面绑定的是 click 事件）
        self.page.locator('button:has-text("同意并申请")').click(force=True)
        self.page.wait_for_timeout(300)

    def get_page_title_text(self) -> str:
        try:
            return self.page.locator('.form-box-title text').first.inner_text(timeout=5000)
        except Exception:
            return self.page.locator('.form-box-title').inner_text(timeout=5000)

    def get_send_btn_text(self) -> str:
        return self.page.locator('button.form-container-send-btn').inner_text(timeout=5000)

    def is_apply_btn_visible(self) -> bool:
        return self.page.locator('button:has-text("同意并申请")').is_visible()

    def get_apply_id_from_storage(self) -> str:
        return self.page.evaluate("localStorage.getItem('applyId') || ''")

    @allure.step("检查页面是否包含指定文本")
    def has_text(self, text: str) -> bool:
        try:
            return text in self.page.inner_text('body', timeout=5000)
        except Exception:
            return False

    @allure.step("获取手机号输入框的值")
    def get_phone_value(self) -> str:
        return self.page.locator('input[name="phone"]').input_value()

    @allure.step("获取车牌号显示文本")
    def get_car_number_text(self) -> str:
        try:
            result = self.page.evaluate("""
                () => {
                    const texts = [];
                    const items = document.querySelectorAll('.car-input-item');
                    items.forEach(item => {
                        const span = item.querySelector('span');
                        const text = item.querySelector('text');
                        const val = item.querySelector('.van-cell__value, .van-field__control');
                        const content = (span || text || val);
                        if (content) {
                            const t = content.textContent || content.value || '';
                            if (t.trim() && t.trim() !== '新能源' && t.trim() !== '_') {
                                texts.push(t.trim());
                            }
                        }
                    });
                    if (texts.length === 0) {
                        const container = document.querySelector('[class*="car-input"], [class*="car-input"]');
                        if (container) {
                            const allSpans = container.querySelectorAll('span');
                            allSpans.forEach(s => {
                                const t = s.textContent.trim();
                                if (t && t !== '新能源') texts.push(t);
                            });
                        }
                    }
                    return texts.join('');
                }
            """)
            return result
        except Exception:
            return ""

    @allure.step("检查是否仍在首页")
    def is_still_on_home_page(self) -> bool:
        return "/home" in self.page.url or self.page.url.rstrip('/').endswith(':9527')

    def screenshot(self) -> bytes:
        return self.page.screenshot()
