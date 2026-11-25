import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("mockpay.app:app", host="0.0.0.0", port=int(os.getenv("MOCKPAY_PORT", "8100")))
