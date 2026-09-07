class SubmitPage:

    def __init__(self, page):
        self.page = page

    def sign_and_submit(self):
        self.page.click("text=去签署")
        self.page.click("text=提交")
