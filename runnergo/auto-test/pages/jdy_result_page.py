import allure
from playwright.sync_api import Page
from pages.base_page import BasePage


class JdyResultPage(BasePage):
    def __init__(self, page: Page):
        super().__init__(page)

    @allure.step("打开借款人信息页")
    def goto(self):
        """直接打开借款人信息页（仅独立测试时使用，端到端流程中由首页跳转到达）"""
        self.page.goto(self.base_url, timeout=30000)
        self.page.wait_for_load_state("domcontentloaded")
        self.page.goto(f"{self.base_url}/result", timeout=30000)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_selector('button:has-text("同意并申请")', timeout=10000)

    @allure.step("选择贷款期限: {term}期")
    def select_loan_term(self, term: int):
        # 优先用 Playwright locator 点击包含"贷款期限"文本的 van-cell
        try:
            label_cell = self.page.locator(
                '.van-cell:has-text("贷款期限")'
            ).first
            label_cell.click(timeout=3000)
        except Exception:
            # JS 兜底
            self.page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null);
                    let node;
                    while (node = walker.nextNode()) {
                        if (node.textContent.trim() === '贷款期限') {
                            node.parentElement.click(); return;
                        }
                    }
                }
            """)
        self.page.wait_for_timeout(300)
        self.select_picker_option(f"{term}期")
        self.confirm_picker()
        self.page.wait_for_timeout(200)

    @allure.step("选择申请地区: {area}")
    def select_area(self, area: str):
        self.page.locator('.van-cell').nth(0).click()
        self.page.wait_for_timeout(300)
        self.wait_for_picker_popup(timeout=5000)
        for part in area.split('-'):
            self.select_picker_option(part)
            self.page.wait_for_timeout(200)
        self.confirm_picker()
        self.page.wait_for_timeout(200)

    @allure.step("选择贷款用途: {use}")
    def select_loan_use(self, use: str):
        self.page.locator('.van-cell').nth(1).click()
        self.page.wait_for_timeout(300)
        self.wait_for_picker_popup(timeout=5000)
        self.select_picker_option(use)
        self.confirm_picker()
        self.page.wait_for_timeout(200)

    @allure.step("验证借款人姓名已填写")
    def verify_name_filled(self) -> bool:
        """OCR 完成后，用 JS 重新读取 DOM 中姓名 input 的 value。
        多策略兜底：van-cell 标签匹配 → input[name=name] → 任意含'姓名'的 input。"""
        try:
            name_val = self.page.evaluate("""
                () => {
                    // 策略1: 通过 van-cell 标签查找
                    const cells = document.querySelectorAll('.van-cell');
                    for (const cell of cells) {
                        const label = cell.querySelector(
                            '.van-cell__title, .van-field__label, label'
                        );
                        if (label && label.textContent.includes('姓名')) {
                            const inp = cell.querySelector('input');
                            if (inp && inp.value.trim()) return inp.value.trim();
                            const val = cell.querySelector('.van-cell__value, .van-field__value');
                            if (val) {
                                const text = (val.textContent || '').trim();
                                if (text && text !== '请输入' && text !== '请选择') return text;
                            }
                        }
                    }
                    // 策略2: 直接找 input[name="name"]
                    const nameInput = document.querySelector('input[name="name"]');
                    if (nameInput && nameInput.value.trim()) return nameInput.value.trim();
                    // 策略3: 找所有 input 中第一个有中文字符值且长度>=2的
                    const allInputs = document.querySelectorAll('input[type="text"], input:not([type])');
                    for (const inp of allInputs) {
                        const val = (inp.value || '').trim();
                        // 中文姓名通常 2-10 个字符
                        if (val.length >= 2 && val.length <= 10 && /[\u4e00-\u9fa5]/.test(val)) {
                            // 排除明显不是姓名的字段（如手机号、身份证号）
                            if (!/^\d+$/.test(val) && val.length !== 11 && val.length !== 18) {
                                return val;
                            }
                        }
                    }
                    return '';
                }
            """)
            allure.attach(f"姓名字段值: '{name_val}'", name="姓名检测")
            return len(name_val.strip()) >= 2
        except Exception as e:
            allure.attach(str(e), name="verify_name_filled 异常")
            return False

    @allure.step("上传身份证正面（OCR识别）")
    def upload_id_front(self, file_path: str):
        self.page.locator('input[type="file"]').first.set_input_files(file_path)
        # 等待 OCR 接口返回（先等网络空闲，再等字段值填充）
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        # 用 wait_for_function 等待姓名字段的 value 不为空（OCR 识别后自动填充）
        try:
            self.page.wait_for_function("""
                () => {
                    const cells = document.querySelectorAll('.van-cell');
                    for (const cell of cells) {
                        const label = cell.querySelector('.van-cell__title, .van-field__label, label');
                        if (label && label.textContent.includes('姓名')) {
                            const inp = cell.querySelector('input');
                            if (inp && inp.value.trim().length >= 2) return true;
                        }
                    }
                    return false;
                }
            """, timeout=8000)
        except Exception:
            allure.attach("OCR姓名字段等待超时，继续执行", name="OCR等待警告")

    @allure.step("上传身份证背面（OCR识别）")
    def upload_id_back(self, file_path: str):
        # 可能有多个 input[type=file]，找到第二个
        file_inputs = self.page.locator('input[type="file"]')
        if file_inputs.count() > 1:
            file_inputs.nth(1).set_input_files(file_path)
        else:
            file_inputs.first.set_input_files(file_path)
        # 等待 OCR 接口返回
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        # 用 wait_for_function 等待签发机关或有效期字段有值
        try:
            self.page.wait_for_function("""
                () => {
                    const inp = document.querySelector('input[name="issueAuthority"]');
                    const ta = document.querySelector('textarea[name="address"]');
                    const hasAuthority = inp && inp.value.trim().length > 0;
                    const hasAddress = ta && ta.value.trim().length > 0;
                    return hasAuthority || hasAddress;
                }
            """, timeout=8000)
        except Exception:
            allure.attach("OCR背面字段等待超时，继续执行", name="OCR等待警告")

    @allure.step("点击同意并申请")
    def click_submit(self):
        # 先关闭可能残留的 picker 弹窗（Vue 兼容方式，绝不 display:none）
        self._close_popup_vue_safe()
        # 用 Playwright click + force=True（与首页一致）
        self.page.locator('button:has-text("同意并申请")').click(force=True)
        self.page.wait_for_timeout(300)

    @allure.step("检查是否出现人车不一致弹窗")
    def has_inconsistency_popup(self) -> bool:
        """检测是否出现人车不一致弹窗"""
        try:
            body_text = self.page.inner_text('body', timeout=3000)
            keywords = ['不一致', '行驶证', '辅助验证', '重新输入']
            return any(kw in body_text for kw in keywords)
        except Exception:
            return False

    @allure.step("上传行驶证辅助验证")
    def upload_driving_license(self, file_path: str):
        """上传行驶证进行辅助验证"""
        # 点击"上传行驶证"按钮
        try:
            self.page.locator('button:has-text("行驶证"), .van-button:has-text("行驶证")').first.click(timeout=5000)
        except Exception:
            # 兜底：用 JS 查找包含"行驶证"的可点击元素
            self.page.evaluate("""() => {
                const els = document.querySelectorAll('button, .van-button, a, span');
                for (const el of els) {
                    if (el.textContent.includes('行驶证')) { el.click(); return; }
                }
            }""")
        self.page.wait_for_timeout(300)

        # 找到文件输入框并上传
        file_inputs = self.page.locator('input[type="file"]')
        count = file_inputs.count()
        if count > 0:
            # 上传到最后一个 file input（行驶证专用的）
            file_inputs.nth(count - 1).set_input_files(file_path)
            allure.attach(f"已上传行驶证: {file_path}", name="行驶证上传")
        else:
            allure.attach("未找到文件输入框", name="行驶证上传失败")

        # 等待校验完成
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(500)

    @allure.step("处理人车不一致弹窗")
    def handle_inconsistency(self, driving_license_path: str) -> bool:
        """处理人车不一致弹窗，上传行驶证辅助验证"""
        if not self.has_inconsistency_popup():
            return False

        allure.attach(self.page.screenshot(), name="人车不一致弹窗", attachment_type=allure.attachment_type.PNG)
        self.upload_driving_license(driving_license_path)
        allure.attach(self.page.screenshot(), name="行驶证上传后", attachment_type=allure.attachment_type.PNG)
        return True

    @allure.step("检查页面是否包含指定文本")
    def has_text(self, text: str) -> bool:
        try:
            return text in self.page.inner_text('body', timeout=5000)
        except Exception:
            return False

    @allure.step("检查是否仍在借款人信息页")
    def is_still_on_result_page(self) -> bool:
        return "/result" in self.page.url

    @allure.step("获取贷款期限值")
    def get_loan_term_value(self) -> str:
        try:
            return self.page.evaluate("""
                () => {
                    const cells = document.querySelectorAll('.van-cell');
                    for (const cell of cells) {
                        const label = cell.querySelector('.van-cell__title, .van-field__label');
                        if (label && label.textContent.includes('贷款期限')) {
                            const val = cell.querySelector('.van-cell__value, .van-field__control');
                            return val ? (val.value || val.textContent).trim() : '';
                        }
                    }
                    return '';
                }
            """)
        except Exception:
            return ""

    def get_current_url(self) -> str:
        return self.page.url

    @allure.step("选择民族")
    def select_ethnicity(self, ethnicity: str):
        """选择民族（van-picker）"""
        self.page.locator('.van-cell:has-text("民族")').first.click()
        self.page.wait_for_timeout(300)
        self.wait_for_picker_popup(timeout=5000)
        self.select_picker_option(ethnicity)
        self.confirm_picker()
        self.page.wait_for_timeout(200)

    @allure.step("填写地址")
    def fill_address(self, address: str):
        """填写地址（textarea）"""
        self.page.locator('textarea[name="address"]').fill(address)
        self.page.wait_for_timeout(300)

    @allure.step("填写签发机关")
    def fill_issue_authority(self, authority: str):
        self.page.locator('input[name="issueAuthority"]').fill(authority)
        self.page.wait_for_timeout(300)

    @allure.step("选择有效日期")
    def select_valid_period(self, period: str):
        """选择有效日期（picker）"""
        self.page.locator('.van-cell:has-text("有效日期")').first.click()
        self.page.wait_for_timeout(300)
        self.wait_for_picker_popup(timeout=5000)
        self.select_picker_option(period)
        self.confirm_picker()
        self.page.wait_for_timeout(200)

    @allure.step("获取身份证号字段的值")
    def get_id_card_value(self) -> str:
        try:
            return self.page.locator('input[name="password"]').nth(1).input_value(timeout=3000)
        except Exception:
            return ""

    @allure.step("覆写借款人身份证号（用于身份证号格式校验测试）")
    def override_borrower_id(self, id_no: str):
        """覆写 OCR 识别后的借款人身份证号字段，用于格式校验测试"""
        # 身份证号字段通常为 input[name="password"] 的第二个（脱敏显示）
        # 也可能用 idCard 等其他名称，多策略兼容
        try:
            loc = self.page.locator('.van-cell:has-text("身份证号") input').first
            if loc.count() > 0:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(id_no, delay=20)
                self.page.wait_for_timeout(200)
                allure.attach(f"已覆写借款人身份证号字段(身份证号标签): {id_no}", name="覆写ID")
                return
        except Exception as e:
            allure.attach(f"标签定位失败: {e}", name="覆写ID警告")
        try:
            # 策略1: input[name="password"] 的第二个
            loc = self.page.locator('input[name="password"]').nth(1)
            if loc.count() > 0:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(id_no, delay=30)
                self.page.wait_for_timeout(300)
                allure.attach(f"已覆写借款人身份证号字段(input[name=password][1]): {id_no}", name="覆写ID")
                return
        except Exception as e:
            allure.attach(f"策略1失败: {e}", name="覆写ID警告")
        try:
            # 策略2: input[name="idCard"] 第一个
            loc = self.page.locator('input[name="idCard"]').first
            if loc.count() > 0:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(id_no, delay=30)
                self.page.wait_for_timeout(300)
                allure.attach(f"已覆写借款人身份证号字段(input[name=idCard]): {id_no}", name="覆写ID")
                return
        except Exception as e:
            allure.attach(f"策略2失败: {e}", name="覆写ID警告")
        try:
            # 策略3: JS 兜底 - 找到所有 input 字段中长度接近18的（脱敏字段）
            # 使用 json.dumps 安全传递参数，避免特殊字符破坏 JS 语法
            import json as _json
            self.page.evaluate("""
                (idNo) => {
                    const inputs = document.querySelectorAll('input');
                    for (const inp of inputs) {
                        const val = inp.value || '';
                        // 脱敏字段一般包含 * 或长度接近18
                        if ((val.includes('*') && val.length >= 15) || val.length === 18) {
                            const proto = HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                            setter.call(inp, idNo);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            return;
                        }
                    }
                }
            """, id_no)
            allure.attach(f"已通过JS覆写借款人身份证号: {id_no}", name="覆写ID")
        except Exception as e:
            allure.attach(f"策略3失败: {e}", name="覆写ID警告")

    @allure.step("获取OCR识别后的姓名值")
    def get_name_value(self) -> str:
        try:
            return self.page.locator('input[name="name"]').input_value(timeout=3000)
        except Exception:
            return ""

    @allure.step("覆写借款人姓名")
    def override_borrower_name(self, name: str):
        """覆写 OCR 识别后的借款人姓名字段，用于姓名正则和一致性校验测试。"""
        try:
            loc = self.page.locator('input[name="name"]').first
            if loc.count() > 0:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(name, delay=20)
                self.page.wait_for_timeout(200)
                allure.attach(f"已覆写借款人姓名: {name}", name="覆写姓名")
                return
        except Exception as e:
            allure.attach(f"原生覆写姓名失败: {e}", name="覆写姓名警告")
        self.page.evaluate("""
            (value) => {
                const inp = document.querySelector('input[name="name"]');
                if (!inp) return;
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                setter.call(inp, value);
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
            }
        """, name)
        allure.attach(f"已通过JS覆写借款人姓名: {name}", name="覆写姓名")

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    @allure.step("上传指定格式图片到身份证正面")
    def upload_id_front_with_format(self, file_path: str):
        """上传图片并检测格式校验结果，用于图片格式校验测试"""
        # 监听上传接口的响应
        try:
            self.page.locator('input[type="file"]').first.set_input_files(file_path)
            try:
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        except Exception as e:
            allure.attach(str(e), name="上传异常")

    @allure.step("上传指定格式图片到行驶证")
    def upload_driving_license_with_format(self, file_path: str):
        """上传图片到行驶证输入框，用于格式校验测试"""
        try:
            self.page.locator('button:has-text("行驶证"), .van-button:has-text("行驶证")').first.click(timeout=3000)
            self.page.wait_for_timeout(300)
        except Exception:
            pass
        file_inputs = self.page.locator('input[type="file"]')
        count = file_inputs.count()
        if count > 2:
            file_inputs.nth(count - 1).set_input_files(file_path)
            try:
                self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            self.page.wait_for_timeout(500)
        else:
            raise RuntimeError("未找到行驶证专用上传入口，不能把行驶证用例上传到身份证输入框")

    @allure.step("获取上传错误提示")
    def get_upload_error(self, timeout: int = 3000) -> str:
        """获取上传后的错误提示（toast/notify/dialog）"""
        self.page.wait_for_timeout(300)
        error = self.page.evaluate("""() => {
            const getVisibleText = (el) => {
                if (!el) return '';
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return '';
                return el.textContent.trim();
            };
            const toasts = document.querySelectorAll('.van-toast');
            for (const t of toasts) { const txt = getVisibleText(t); if (txt) return txt; }
            const notifies = document.querySelectorAll('.van-notify');
            for (const n of notifies) { const txt = getVisibleText(n); if (txt) return txt; }
            const dialogs = document.querySelectorAll('.van-dialog__message');
            for (const d of dialogs) { const txt = getVisibleText(d); if (txt) return txt; }
            return '';
        }""")
        return error
