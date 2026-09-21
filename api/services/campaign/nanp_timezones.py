"""North American area code to IANA timezone table.

Used to judge a lead's *local* time — calling windows are defined in the called
party's time, not the caller's — and to pick a caller ID that looks local to
them.

Codes are grouped by the region that assigns them so the table is reviewable;
``NANP_AREA_CODE_TIMEZONES`` is flattened from those groups and checked for
duplicates by the test suite.

Some area codes genuinely straddle a timezone boundary (Florida's 850, Indiana's
219, Texas's 915, Idaho's 208). Those are listed in ``AMBIGUOUS_AREA_CODES`` and
mapped to the zone holding most of their population. A lead list that needs
exactness should carry its own ``timezone`` value, which always wins over this
table.
"""

from __future__ import annotations

EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"  # Mountain time, no DST
PACIFIC = "America/Los_Angeles"
ALASKA = "America/Anchorage"
HAWAII = "Pacific/Honolulu"
ATLANTIC_CANADA = "America/Halifax"
NEWFOUNDLAND = "America/St_Johns"
SASKATCHEWAN = "America/Regina"  # Central time, no DST

# Area codes whose territory spans more than one timezone. Mapped to the
# majority zone; see the module docstring.
AMBIGUOUS_AREA_CODES = frozenset({"208", "219", "406", "850", "906", "915", "986"})

_GROUPS: list[tuple[str, str]] = [
    # ---- United States -------------------------------------------------
    (
        EASTERN,
        """
        202 203 207 212 215 216 219 220 223 227 229 231 234 239 240 248 252 267 269
        272 276 283 289 301 302 304 305 313 315 317 321 324 326 329 330 332 336 339
        347 351 352 363 364 380 386 401 404 407 410 412 413 419 423 434 436 440 443
        445 448 463 464 470 475 478 484 502 508 513 516 517 518 540 561 567 570
        571 582 585 586 603 606 607 610 614 616 617 624 626 629 631 640 645 646 656
        667 678 679 680 681 686 689 703 704 706 716 717 718 724 727 732 734 740 743
        754 757 762 765 770 771 772 774 781 786 802 803 804 810 812 813 821 826 828
        835 838 839 843 845 848 854 856 857 859 862 863 864 865 878 904 906 908 910
        912 914 917 919 929 930 934 937 941 943 947 948 954 959 973 978 980 984 989
    """,
    ),
    (
        CENTRAL,
        """
        205 210 214 217 218 224 225 228 235 251 254 256 262 270 274 281 308 309 312
        314 316 318 319 320 325 331 337 346 361 367 402 405 409 414 417 430 431 432
        447 469 479 501 504 507 512 515 531 534 539 557 563 572 573 580 584 601 605
        608 612 615 618 620 630 636 641 651 659 660 662 682 701 708 712 713 715 726
        730 731 737 763 769 779 785 806 815 816 817 830 832 847 850 861 870 872 901
        903 913 915 918 920 931 936 938 940 945 952 956 972 975 979 985
    """,
    ),
    (
        MOUNTAIN,
        """
        208 303 307 385 406 505 575 719 720 748 801 970 983 986
    """,
    ),
    (ARIZONA, "480 520 602 623 928"),
    (
        PACIFIC,
        """
        206 209 213 253 279 310 323 341 350 360 369 408 415 424 425 442 458 503 509
        510 530 541 559 562 564 619 628 650 657 661 669 702 707 714 725 738 747 760
        764 775 805 818 831 837 840 858 909 916 925 949 951 971
    """,
    ),
    (ALASKA, "907"),
    (HAWAII, "808"),
    # ---- Canada --------------------------------------------------------
    (
        EASTERN,
        """
        226 249 289 343 365 368 416 418 437 438 450 468 514 519 548 579 581 613 647
        683 705 742 753 807 819 873 905 942
    """,
    ),
    (CENTRAL, "204 431 584"),
    (SASKATCHEWAN, "306 474 639"),
    (MOUNTAIN, "403 587 780 825 867"),
    (PACIFIC, "236 250 604 672 778"),
    (ATLANTIC_CANADA, "506 782 902"),
    (NEWFOUNDLAND, "709"),
    # ---- Caribbean and Pacific territories -----------------------------
    ("America/Puerto_Rico", "787 939"),
    ("America/St_Thomas", "340"),
    ("America/Santo_Domingo", "809 829 849"),
    ("America/Jamaica", "658 876"),
    ("America/Nassau", "242"),
    ("Atlantic/Bermuda", "441"),
    ("America/Cayman", "345"),
    ("America/Barbados", "246"),
    ("America/Port_of_Spain", "868"),
    ("America/Grand_Turk", "649"),
    ("Pacific/Guam", "671"),
    ("Pacific/Saipan", "670"),
    ("Pacific/Pago_Pago", "684"),
]


def _build() -> dict[str, str]:
    table: dict[str, str] = {}
    for timezone, codes in _GROUPS:
        for code in codes.split():
            table[code] = timezone
    return table


NANP_AREA_CODE_TIMEZONES: dict[str, str] = _build()


def duplicate_area_codes() -> dict[str, list[str]]:
    """Area codes claimed by more than one group, with the zones that claim them.

    Used by the test suite to keep the table honest: a duplicate means one
    group silently overwrote another and some leads get the wrong local time.
    """
    seen: dict[str, list[str]] = {}
    for timezone, codes in _GROUPS:
        for code in codes.split():
            seen.setdefault(code, []).append(timezone)
    return {code: zones for code, zones in seen.items() if len(set(zones)) > 1}
