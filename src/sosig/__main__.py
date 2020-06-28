import aiorun

import sosig


def main() -> None:
    aiorun.run(sosig.main(), stop_on_unhandled_errors=True)


if __name__ == "__main__":
    main()
