"""一个有典型问题的示例文件，用来演示 --code 模式。"""


def average(total, count):
    return total / count#count==0


def add_item(item, items=[]):
    items.append(item)
    return items


def get_score(text):
    try:
        return int(text)#maybe text not an integer
    except:
        pass


def read_config(path):
    f = open(path)
    return f.read()#path not found
