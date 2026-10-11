# 这边是 Python 的基础知识

## 基础知识

with 的作用：
    本质是进入和退出代码块的时候
        __enter__()
        __exit__()
    异步代码块
        __aenter__()
        __aexit__()
        异步代码块内部发生异常：
            向外抛出异常
            但是如果先发生退出，那么通常向外抛出 TimeoutError

代码参数的内容：
    def func(self, *, name: str):
        if type(name) is not str:
        这边要做一层校验，只是类型注解，不会强制检查内容
