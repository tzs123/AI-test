import allure
from playwright.sync_api import Page
from pages.base_page import BasePage


class JdySubmitPage(BasePage):
    def __init__(self, page: Page):
        super().__init__(page)

    def _sign_button(self):
        visible_btn = self.page.locator('button:has-text("去签署"):visible')
        try:
            if visible_btn.count() > 0:
                return visible_btn.last
        except Exception:
            pass
        return self.page.locator('button:has-text("去签署")').last

    @allure.step("等待签署页加载完成")
    def wait_for_submit_page(self, timeout: int = 15000):
        """等待页面导航到签署页（由前置流程跳转而来，不直接 goto）"""
        self.page.wait_for_url("**/submit**", timeout=timeout)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(2000)

    @allure.step("验证申请信息显示")
    def verify_apply_info(self, borrower_name: str = "") -> bool:
        try:
            content = self.page.inner_text('body')
            if borrower_name:
                return borrower_name in content
            return '申请信息' in content
        except Exception:
            return False

    @allure.step("获取各步骤状态")
    def get_step_status(self) -> dict:
        """
        debug 输出确认文本结构：
        额度评估\n基于你的车辆信息\n已完成\n资格审核\n...
        """
        try:
            content = self.page.inner_text('body')
        except Exception:
            return {}
        steps = ['额度评估', '资格审核', '资料补充', '协议签署']
        result = {}
        for step in steps:
            idx = content.find(step)
            if idx == -1:
                result[step] = '未知'
                continue
            # 取步骤名后250字符，判断最近的状态文字
            segment = content[idx: idx + 250]
            if '已完成' in segment:
                result[step] = '已完成'
            elif '未完成' in segment:
                result[step] = '未完成'
            else:
                result[step] = '未知'
        allure.attach(
            f"步骤状态: {result}\n\n原文片段:\n{content[:600]}",
            name="签署页状态检测"
        )
        return result

    @allure.step("点击去签署")
    def click_sign(self):
        self._sign_button().click()
        self.page.wait_for_timeout(2000)

    @allure.step("点击去签署并验证进入签署协议页")
    def click_sign_and_verify(self, timeout: int = 10000) -> bool:
        """点击【去签署】按钮，验证页面是否进入签署协议页面。

        签署协议页的特征：
        - URL 可能变为 /sign 或 /agreement 或 /contract
        - 页面出现"签署协议"、"协议签署"、"勾选并签署"等文本
        - 或出现协议勾选框、签署按钮等元素

        返回 True 表示已进入签署协议页，False 表示未进入。
        """
        # 点击前记录当前 URL
        old_url = self.page.url
        allure.attach(f"点击前URL: {old_url}", name="签署前URL")
        try:
            old_body_text = self.page.inner_text('body', timeout=2000)
        except Exception:
            old_body_text = ""
        sign_responses = []
        self.last_sign_responses = []
        self.last_sign_error = ""

        def _record_sign_response(resp):
            url = resp.url
            if not any(kw in url for kw in ("/submit/sign", "/sign", "sign", "Sign")):
                return
            try:
                body = resp.text()[:800]
            except Exception as e:
                body = f"<读取响应体失败: {e}>"
            sign_responses.append(f"{resp.status} {url}\n{body}")

        try:
            self.page.on("response", _record_sign_response)
        except Exception:
            pass

        # 点击【去签署】按钮
        try:
            btn = self._sign_button()
            if btn.count() == 0:
                allure.attach("未找到'去签署'按钮", name="签署验证")
                return False
            # 检查按钮是否可点击
            disabled = btn.evaluate("""el => {
                if (el.disabled) return true;
                const cls = el.className || '';
                if (cls.includes('disabled') || cls.includes('van-button--disabled')) return true;
                return false;
            }""")
            if disabled:
                allure.attach("'去签署'按钮处于禁用状态，无法点击", name="签署验证")
                return False
            btn.scroll_into_view_if_needed(timeout=3000)
            try:
                btn.click(timeout=5000)
            except Exception:
                btn.click(force=True, timeout=5000)
        except Exception as e:
            allure.attach(f"点击'去签署'按钮异常: {e}", name="签署验证")
            return False

        # 等待页面变化（URL 变化 或 新元素出现）
        import time as _time
        start = _time.time()
        entered = False
        while _time.time() - start < timeout / 1000:
            new_url = self.page.url
            body_text = ""
            try:
                body_text = self.page.inner_text('body', timeout=2000)
            except Exception:
                pass

            # 判断是否进入签署协议页
            # 1. URL 变化（包含 sign/agreement/contract）
            if new_url != old_url and any(kw in new_url.lower() for kw in ["sign", "agreement", "contract", "protocol"]):
                entered = True
                break
            # 2. 页面出现签署协议相关文本
            sign_keywords = ["签署协议", "协议签署", "勾选并签署", "阅读并同意", "我已阅读", "确认签署", "签署合同", "电子签约"]
            has_new_sign_text = any(kw in body_text and kw not in old_body_text for kw in sign_keywords)
            if new_url != old_url and any(kw in body_text for kw in sign_keywords):
                entered = True
                break
            if has_new_sign_text:
                entered = True
                break
            # 3. 出现协议勾选框或新的签署按钮
            try:
                has_checkbox = self.page.locator('input[type="checkbox"], .van-checkbox, .van-radio').count()
                has_sign_btn = self.page.locator('button:has-text("确认签署"), button:has-text("同意并签署"), button:has-text("立即签署")').count()
                if (new_url != old_url and (has_checkbox > 0 or has_sign_btn > 0)):
                    entered = True
                    break
            except Exception:
                pass

            self.page.wait_for_timeout(300)

        try:
            self.page.remove_listener("response", _record_sign_response)
        except Exception:
            pass

        try:
            click_error = self.page.evaluate("""() => {
                const visibleText = (el) => {
                    if (!el) return '';
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return '';
                    return (el.textContent || '').trim();
                };
                const selectors = ['.van-toast', '.van-notify', '.van-dialog__message', '[class*="error"]'];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const text = visibleText(el);
                        if (text) return text;
                    }
                }
                return '';
            }""")
        except Exception:
            click_error = ""
        self.last_sign_responses = list(sign_responses)
        self.last_sign_error = click_error

        allure.attach(self.page.screenshot(), name="点击去签署后", attachment_type=allure.attachment_type.PNG)
        allure.attach(
            "\n\n".join(sign_responses) or "未捕获到签署相关响应",
            name="签署点击网络响应",
            attachment_type=allure.attachment_type.TEXT,
        )
        allure.attach(click_error or "未检测到页面错误提示", name="签署点击错误提示")
        allure.attach(
            f"点击后URL: {self.page.url}\n是否进入签署协议页: {entered}",
            name="签署协议验证结果",
            attachment_type=allure.attachment_type.TEXT
        )
        return entered

    @allure.step("从签署协议页返回签署页")
    def back_to_submit(self, timeout: int = 10000) -> bool:
        """从签署协议页返回 /submit。优先用页面返回，其次用浏览器后退。"""
        selectors = [
            '.van-nav-bar__left',
            '.van-icon-arrow-left',
            'button:has-text("返回")',
            '.nav-back',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=1000):
                    el.click(force=True)
                    self.page.wait_for_timeout(500)
                    if "/submit" in self.page.url:
                        return True
                    break
            except Exception:
                pass

        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            pass

        import time as _time
        start = _time.time()
        while _time.time() - start < timeout / 1000:
            if "/submit" in self.page.url:
                return True
            self.page.wait_for_timeout(200)
        return "/submit" in self.page.url

    def get_current_url(self) -> str:
        return self.page.url

    @allure.step("检查页面是否包含指定文本")
    def has_text(self, text: str) -> bool:
        try:
            return text in self.page.inner_text('body', timeout=5000)
        except Exception:
            return False

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    @allure.step("点击返回上一页")
    def click_back(self):
        """点击签署页的返回按钮，返回到补充信息页"""
        # 尝试多种返回按钮定位方式
        selectors = [
            '.van-nav-bar__left',
            '.van-icon-arrow-left',
            'button:has-text("返回")',
            '.nav-back',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click(force=True)
                    self.page.wait_for_timeout(1500)
                    return
            except Exception:
                pass
        # 兜底：浏览器后退
        self.page.go_back()
        self.page.wait_for_timeout(1500)

    @allure.step("检查签署按钮是否可点击")
    def is_sign_btn_enabled(self) -> bool:
        """检查'去签署'按钮是否可点击（未禁用）"""
        try:
            btn = self._sign_button()
            if btn.count() == 0:
                return False
            # 检查是否有 disabled 类或属性
            disabled = btn.evaluate("""el => {
                if (el.disabled) return true;
                const cls = el.className || '';
                if (cls.includes('disabled') || cls.includes('van-button--disabled')) return true;
                const style = window.getComputedStyle(el);
                if (style.pointerEvents === 'none') return true;
                if (style.opacity === '0.5' || parseFloat(style.opacity) < 1) return true;
                return false;
            }""")
            return not disabled
        except Exception:
            return False

    @allure.step("检查签署按钮是否存在")
    def has_sign_btn(self) -> bool:
        """检查页面是否有'去签署'按钮"""
        try:
            return self.page.locator('button:has-text("去签署"):visible').count() > 0
        except Exception:
            return False

    @allure.step("返回首页")
    def back_to_home(self, base_url: str = ""):
        """从签署页返回首页"""
        if base_url:
            self.page.goto(f"{base_url}/home?channelId=JDYFWH&productId=JDYPRD01", timeout=15000)
        else:
            # 浏览器后退，直到回到首页
            for _ in range(5):
                self.page.go_back()
                self.page.wait_for_timeout(1500)
                if "/home" in self.page.url:
                    break
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        self.page.wait_for_timeout(1500)
