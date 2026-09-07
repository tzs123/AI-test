class ResultPage:

    def __init__(self, page):
        self.page = page

    def wait_load(self):
        self.page.wait_for_url("**/result")

    def get_limit(self):
        return self.page.text_content("text=/\\d+/,")
