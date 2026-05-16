import yfinance as yf

def main():
    ticker = yf.Ticker("AAPL")
    targets = ticker.get_recommendations_summary()
    print(targets)

# This tells Python to actually run the main() function
if __name__ == "__main__":
    main()