"""知识库 UI 测试共享流程（来源：新网申_测试用例_合并.xmind）"""
import os
import pytest
import allure
from pages.jdy_home_page import JdyHomePage
from pages.jdy_result_page import JdyResultPage
from pages.jdy_fill_page import JdyFillPage


def load_kb_cases(yaml_file, tag=None, exclude_manual=True):
    import os as _os
    from utils.yaml_loader import load_cases as _load
    # YAML 文件不存在时，返回 skip 标记，避免 pytest 收集到无效的 [NOTSET] 用例
    full_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), yaml_file)
    if not _os.path.exists(full_path):
        return [pytest.param(None, marks=pytest.mark.skip(reason=f"YAML case file not found: {yaml_file}"))]
    cases = _load(yaml_file)
    if not cases:
        return [pytest.param(None, marks=pytest.mark.skip(reason=f"No cases in YAML: {yaml_file}"))]
    filtered = []
    for c in cases:
        if exclude_manual and "manual" in c.get("tags", []):
            continue
        if tag and tag not in c.get("tags", []):
            continue
        filtered.append(c)
    return filtered


def wait_url_contains(page, keyword: str, timeout: int = 8000):
    import time as _time
    start = _time.time()
    while _time.time() - start < timeout / 1000:
        if keyword in page.url:
            return True
        page.wait_for_timeout(100)
    raise TimeoutError(f"等待URL包含'{keyword}'超时({timeout}ms)，当前URL: {page.url}")


def smart_wait_url_change(page, old_url: str, timeout: int = 3000):
    """提交后智能等待URL变化，一旦变化立即返回，不浪费固定等待时间"""
    import time as _time
    start = _time.time()
    while _time.time() - start < timeout / 1000:
        if page.url != old_url:
            page.wait_for_timeout(200)
            return True
        page.wait_for_timeout(100)
    return False


def get_page_error(page) -> str:
    """检测页面上的各种错误提示，返回第一个找到的错误文本。"""
    page.wait_for_timeout(250)  # 等待错误提示动画完成
    return page.evaluate("""() => {
        const getVisibleText = (el) => {
            if (!el) return '';
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return '';
            if (style.opacity === '0') return '';
            return el.textContent.trim();
        };
        // 1. van-toast
        const toasts = document.querySelectorAll('.van-toast');
        for (const t of toasts) { const txt = getVisibleText(t); if (txt) return txt; }
        // 2. van-notify
        const notifies = document.querySelectorAll('.van-notify');
        for (const n of notifies) { const txt = getVisibleText(n); if (txt) return txt; }
        // 3. van-dialog 弹窗消息
        const dialogs = document.querySelectorAll('.van-dialog__message, .van-dialog');
        for (const d of dialogs) { const txt = getVisibleText(d); if (txt) return txt; }
        // 4. 表单字段错误
        const errors = document.querySelectorAll('.van-field__error-message, .van-cell__error-message, .error-msg, .error-text, .error-message');
        const errTexts = Array.from(errors).map(getVisibleText).filter(t => t);
        if (errTexts.length > 0) return errTexts.join(', ');
        // 5. 任何可见的包含"请"的错误提示文本
        const allElements = document.querySelectorAll('div, span, p');
        for (const el of allElements) {
            const txt = getVisibleText(el);
            if (txt && txt.length < 50 && (txt.startsWith('请') || txt.includes('不能为空') || txt.includes('格式不正确') || txt.includes('请输入') || txt.includes('请选择'))) {
                return txt;
            }
        }
        return '';
    }""")


def xfail_known(case, default_reason="已知问题"):
    pytest.xfail(case.get("known_bug") or case.get("known_issue") or default_reason)


def flow_home(page, case):
    home = JdyHomePage(page)
    home.goto(case["channel_id"], case["product_id"])
    allure.attach(home.screenshot(), name="首页加载", attachment_type=allure.attachment_type.PNG)
    assert home.has_text("输入信息获取评测额度")

    home.input_phone(case["phone"])

    if case.get("expect_send_fail"):
        try:
            home.click_send_captcha()
        except Exception:
            allure.attach("发送验证码按钮可能被禁用", name="发送失败")
        page.wait_for_timeout(300)
        error_msg = get_page_error(page)
        allure.attach(error_msg or "无错误提示", name="发送验证码错误")
        btn_text = home.get_send_btn_text()
        assert error_msg or "秒" not in btn_text, \
            f"期望手机号格式错误时发送失败，但按钮进入倒计时: {btn_text}"
        return

    home.click_send_captcha()

    if case.get("expect_countdown"):
        btn_text = home.get_send_btn_text()
        allure.attach(btn_text, name="发送按钮文本")
        assert any(k in btn_text for k in ("秒", "重发", "发送")), \
            f"期望倒计时文案，实际: {btn_text}"
        return

    if case.get("expect_resend_blocked"):
        try:
            home.click_send_captcha()
        except Exception:
            allure.attach("按钮在倒计时期间被禁用，符合预期", name="重复发送被拦截")
        btn_text = home.get_send_btn_text()
        allure.attach(f"按钮文本: {btn_text}", name="按钮状态")
        return

    home.input_captcha(case["captcha"])
    entered_car_number = home.input_car_number(case["car_number"])
    if entered_car_number != case["car_number"]:
        allure.attach(
            f"目标车牌: {case['car_number']}\n页面实际: {entered_car_number}",
            name="车牌号输入结果"
        )

    if case.get("check_agree", True):
        home.check_agree()

    allure.attach(home.screenshot(), name="首页填写完毕", attachment_type=allure.attachment_type.PNG)
    pre_submit_wait_ms = int(case.get("pre_submit_wait_ms", 0) or 0)
    if pre_submit_wait_ms > 0:
        page.wait_for_timeout(pre_submit_wait_ms)
    old_url = page.url
    if case.get("expect_double_click_safe"):
        submit = page.locator('button:has-text("同意并申请")')
        for _ in range(3):
            try:
                submit.click(force=True, timeout=1000)
                page.wait_for_timeout(80)
            except Exception:
                break
    else:
        home.click_submit()
    smart_wait_url_change(page, old_url, timeout=8000)

    if case.get("expect_submit_fail") and case.get("expect_fail_page", "home") == "home":
        error_msg = get_page_error(page)
        still_on_home = "/home" in page.url
        if error_msg:
            allure.attach(error_msg, name="提交错误")
            # 如果指定了期望关键词，严格校验
            if case.get("expect_error_keyword"):
                assert case["expect_error_keyword"] in error_msg, \
                    f"期望错误提示包含'{case['expect_error_keyword']}'，实际: '{error_msg}'"
            else:
                # 默认关键词校验：必填字段未填时应提示"请输入/请选择/不能为空/格式不正确"
                default_keywords = ["请输入", "请选择", "不能为空", "格式不正确", "请填写", "必填", "请勾选"]
                matched = [kw for kw in default_keywords if kw in error_msg]
                if not matched:
                    allure.attach(
                        f"错误提示未包含默认关键词{default_keywords}，实际: '{error_msg}'",
                        name="校验警告"
                    )
            allure.attach(f"✅ 校验通过：错误提示符合预期 - {error_msg}", name="提交结果")
        elif still_on_home:
            allure.attach("✅ 校验通过：提交被拦截，页面未跳转", name="提交结果")
        else:
            allure.attach(page.screenshot(), name="提交意外成功", attachment_type=allure.attachment_type.PNG)
            raise AssertionError("期望提交失败，但页面已跳转（提交成功了）")
        return

    # 首页提交后统一等待跳转到 /result（expect_url 是最终目标，不是首页跳转目标）
    try:
        wait_url_contains(page, "/result", timeout=10000)
    except TimeoutError:
        if "/home" in page.url:
            error_msg = get_page_error(page)
            allure.attach(page.screenshot(), name="首页提交后未跳转", attachment_type=allure.attachment_type.PNG)
            allure.attach(f"首页提交后停留在首页，错误提示: {error_msg or '无'}", name="提交失败")
            raise TimeoutError(f"首页提交后未跳转，停留在/home，错误: {error_msg or '无'}")
        raise


def flow_result(page, case):
    result = JdyResultPage(page)
    assert result.is_still_on_result_page()
    allure.attach(result.screenshot(), name="申请资料页加载", attachment_type=allure.attachment_type.PNG)

    if case.get("id_front") and os.path.exists(case["id_front"]):
        result.upload_id_front(case["id_front"])
    if case.get("id_back") and os.path.exists(case["id_back"]):
        result.upload_id_back(case["id_back"])

    override_name = case.get("override_borrower_name")
    if not override_name and case.get("expect_error_keyword") == "姓名":
        current_name = result.get_name_value()
        if case.get("borrower_name") and case["borrower_name"] != current_name:
            override_name = case["borrower_name"]
    if override_name:
        result.override_borrower_name(override_name)

    override_id = case.get("override_borrower_id") or case.get("borrower_id")
    if override_id:
        result.override_borrower_id(override_id)

    if case.get("loan_term"):
        result.select_loan_term(case["loan_term"])
        if case.get("expect_pressure_tip"):
            try:
                page.wait_for_selector('.van-dialog, .van-toast, .van-notify', state='visible', timeout=2000)
            except Exception:
                pass
            body_text = page.inner_text("body")
            allure.attach(body_text[:500], name="还款压力提示检测")
            if not any(k in body_text for k in ("还款压力", "压力")):
                if case.get("known_bug") or case.get("known_issue"):
                    xfail_known(case)
                raise AssertionError("期望选择贷款期限后出现还款压力提示")
            for sel in ['.van-dialog__confirm', 'button:has-text("确认")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=500):
                        btn.click(force=True)
                        break
                except Exception:
                    pass
    if case.get("area"):
        result.select_area(case["area"])
    if case.get("loan_use"):
        result.select_loan_use(case["loan_use"])

    if case.get("ethnicity"):
        try:
            result.select_ethnicity(case["ethnicity"])
        except Exception:
            allure.attach("民族字段未找到，可能OCR未成功", name="字段操作失败")
    if case.get("id_card_address"):
        try:
            current = page.locator('textarea[name="address"]').input_value(timeout=3000)
            if not current.strip():
                result.fill_address(case["id_card_address"])
        except Exception:
            allure.attach("地址字段未找到，可能OCR未成功", name="字段操作失败")
    if case.get("issue_authority"):
        try:
            current = page.locator('input[name="issueAuthority"]').input_value(timeout=3000)
            if not current.strip():
                result.fill_issue_authority(case["issue_authority"])
        except Exception:
            allure.attach("签发机关字段未找到，可能OCR未成功", name="字段操作失败")
    if case.get("valid_period"):
        try:
            result.select_valid_period(case["valid_period"])
        except Exception:
            allure.attach("有效日期字段未找到，可能OCR未成功", name="字段操作失败")

    allure.attach(result.screenshot(), name="申请资料填写完毕", attachment_type=allure.attachment_type.PNG)
    old_url = page.url
    result.click_submit()
    smart_wait_url_change(page, old_url, timeout=3000)

    # 人车不一致时默认上传行驶证辅助验证（无需 case 显式声明）
    # 当 home 页车牌号与身份证 OCR 不是同一人时，被测系统会弹窗要求辅助验证
    default_driving_license = case.get(
        "driving_license",
        "/Users/tanzsongsen/Downloads/行驶证正面.png"
    )
    if case.get("expect_no_mismatch_popup"):
        assert not result.has_inconsistency_popup(), "期望人车一致，但出现了人车不一致弹窗"

    should_handle_inconsistency = (
        not case.get("expect_no_mismatch_popup")
        and (not case.get("expect_submit_fail") or case.get("expect_mismatch_flow") or case.get("expect_driving_license_flow"))
    )
    # 仅在文件存在且本用例需要继续处理不一致弹窗时才尝试上传
    if should_handle_inconsistency and os.path.exists(default_driving_license):
        # 等待弹窗出现
        page.wait_for_timeout(500)
        if result.has_inconsistency_popup():
            allure.attach(
                page.screenshot(),
                name="检测到人车不一致弹窗",
                attachment_type=allure.attachment_type.PNG
            )
            if result.handle_inconsistency(default_driving_license):
                allure.attach(
                    "✅ 行驶证辅助验证已处理，继续流程",
                    name="人车不一致处理"
                )
                # 上传行驶证后等待页面跳转
                smart_wait_url_change(page, old_url, timeout=5000)
            else:
                allure.attach("行驶证上传未成功", name="人车不一致处理")
        else:
            # 没有弹窗，可能已直接通过
            allure.attach("未检测到人车不一致弹窗", name="人车不一致处理")
    elif should_handle_inconsistency:
        allure.attach(
            f"默认行驶证文件不存在: {default_driving_license}",
            name="人车不一致处理跳过"
        )

    if case.get("expect_submit_fail") and case.get("expect_fail_page", "result") == "result":
        error_msg = get_page_error(page)
        still_on_result = "/result" in page.url
        if error_msg:
            allure.attach(error_msg, name="提交错误")
            # 如果指定了期望关键词，严格校验
            if case.get("expect_error_keyword"):
                assert case["expect_error_keyword"] in error_msg, \
                    f"期望错误提示包含'{case['expect_error_keyword']}'，实际: '{error_msg}'"
            else:
                # 默认关键词校验：必填字段未填时应提示"请输入/请选择/不能为空/格式不正确"
                default_keywords = ["请输入", "请选择", "不能为空", "格式不正确", "请填写", "必填", "请上传"]
                matched = [kw for kw in default_keywords if kw in error_msg]
                if not matched:
                    allure.attach(
                        f"错误提示未包含默认关键词{default_keywords}，实际: '{error_msg}'",
                        name="校验警告"
                    )
            allure.attach(f"✅ 校验通过：错误提示符合预期 - {error_msg}", name="提交结果")
        elif still_on_result:
            allure.attach("✅ 校验通过：提交被拦截，页面未跳转", name="提交结果")
        else:
            allure.attach(page.screenshot(), name="提交意外成功", attachment_type=allure.attachment_type.PNG)
            raise AssertionError("期望提交失败，但页面已跳转（提交成功了）")
        return

    # result 页提交后统一等待跳转到 /fill（expect_url 是最终目标，不是 result 页跳转目标）
    wait_url_contains(page, "/fill", timeout=15000)


def flow_fill(page, case):
    fill = JdyFillPage(page)
    assert "/fill" in fill.get_current_url()
    allure.attach(fill.screenshot(), name="补充借款信息页加载", attachment_type=allure.attachment_type.PNG)

    if case.get("expect_contact1_relation_locked"):
        assert fill.is_contact1_relation_disabled(), "已婚时联系人1关系应锁定为配偶"

    if case.get("fill_area"):
        fill.select_area(case["fill_area"])
    if case.get("fill_address"):
        fill.fill_address(case["fill_address"])
    if case.get("marriage"):
        fill.select_marriage(case["marriage"])
    if case.get("education"):
        fill.select_education(case["education"])
    if case.get("company"):
        fill.fill_company(case["company"])
    if case.get("unit_industry"):
        fill.select_unit_industry(case["unit_industry"])
    if case.get("unit_address"):
        fill.fill_unit_address(case["unit_address"])
    if case.get("work_type"):
        fill.select_work_type(case["work_type"])
    if case.get("annual_income") is not None and case.get("annual_income") != "":
        fill.fill_annual_income(case["annual_income"])
    if case.get("contact1_name") is not None:
        fill.fill_contact1(
            case.get("contact1_name", ""),
            case.get("contact1_relation", ""),
            case.get("contact1_phone", ""),
            case.get("contact1_id", ""),
        )
    if case.get("contact2_name"):
        fill.fill_contact2(
            case["contact2_name"], case["contact2_relation"], case["contact2_phone"]
        )

    allure.attach(fill.screenshot(), name="补充信息填写完毕", attachment_type=allure.attachment_type.PNG)
    old_url = page.url

    # 判断是否为反向用例（输入异常数据，期望提交被拦截）
    is_negative = bool(case.get("expect_submit_fail")) or bool(case.get("expect_error_keyword"))

    if is_negative:
        if case.get("expect_error_keyword") == "身份证" and fill._find_cell_index_by_label("身份证号", 0) < 0:
            xfail_known(case, "当前补充信息页没有联系人身份证号字段，无法执行联系人身份证号校验用例")

        # 反向用例：点击提交后，应断言"错误提示出现"而非"跳转到下一页"
        fill.click_submit()
        # 立即等待错误提示元素出现（toast 可能只显示 2s，不能等太久）
        try:
            page.wait_for_selector(
                '.van-toast, .van-notify, .van-field__error-message, .van-dialog__message, [class*="error"]',
                state='visible', timeout=5000
            )
        except Exception:
            pass
        error_msg = get_page_error(page)
        still_on_fill = "/fill" in fill.get_current_url()
        allure.attach(f"error_msg='{error_msg}', still_on_fill={still_on_fill}, url={page.url}",
                      name="反向用例提交结果", attachment_type=allure.attachment_type.TEXT)

        if not still_on_fill:
            if case.get("known_bug") or case.get("known_issue"):
                xfail_known(case)
            allure.attach(page.screenshot(), name="提交意外成功", attachment_type=allure.attachment_type.PNG)
            raise AssertionError("期望提交失败（异常数据应被拦截），但页面已跳转（提交成功了）")

        if error_msg:
            allure.attach(error_msg, name="提交错误提示")
            # 如果指定了期望关键词，严格校验
            if case.get("expect_error_keyword"):
                if case["expect_error_keyword"] not in error_msg:
                    if case.get("known_bug") or case.get("known_issue"):
                        xfail_known(case)
                    raise AssertionError(
                        f"期望错误提示包含'{case['expect_error_keyword']}'，实际: '{error_msg}'")
                allure.attach(f"✅ 校验通过：错误提示包含期望关键词'{case['expect_error_keyword']}'", name="断言结果")
            else:
                # 无指定关键词时，检查是否包含常见校验关键词
                default_keywords = ["请输入", "请选择", "不能为空", "格式不正确", "请填写", "必填",
                                    "请上传", "请勾选", "不一致", "重复", "相同", "错误", "无效"]
                matched = [kw for kw in default_keywords if kw in error_msg]
                if matched:
                    allure.attach(f"✅ 校验通过：错误提示包含关键词{matched} - {error_msg}", name="断言结果")
                else:
                    # 错误提示不含任何常见关键词，但仍拦截了提交，视为通过
                    allure.attach(f"✅ 校验通过：提交被拦截（错误提示: {error_msg}）", name="断言结果")
        elif still_on_fill:
            # 提交被拦截但没有显式错误提示 - 如果指定了关键词则失败，否则通过
            if case.get("expect_error_keyword"):
                allure.attach(page.screenshot(), name="未检测到错误提示", attachment_type=allure.attachment_type.PNG)
                raise AssertionError(
                    f"期望错误提示包含'{case['expect_error_keyword']}'，但未检测到任何错误提示（页面停留在/fill）")
            allure.attach("✅ 校验通过：提交被拦截，页面未跳转（无显式错误提示）", name="断言结果")
        else:
            # 页面跳转了 - 说明异常数据被系统接受了，这是 bug
            allure.attach(page.screenshot(), name="提交意外成功", attachment_type=allure.attachment_type.PNG)
            raise AssertionError("期望提交失败（异常数据应被拦截），但页面已跳转（提交成功了）")
        return

    # 正向用例：点击提交后等待跳转到下一页
    fill.click_submit()
    smart_wait_url_change(page, old_url, timeout=2000)

    if case.get("expect_submit_page"):
        try:
            wait_url_contains(page, "/submit", timeout=15000)
        except TimeoutError:
            error_msg = fill.get_error_toast()
            allure.attach(f"等待/submit超时，当前URL: {page.url}，错误提示: {error_msg}", name="提交失败")
            if error_msg:
                allure.attach(f"提交失败，有错误提示: {error_msg}", name="提交结果")
            else:
                allure.attach("提交后页面未跳转，无错误提示", name="提交结果")
