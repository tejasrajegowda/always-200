import always_200


def test_package_imports_and_has_a_version():
    assert always_200.__version__ == "0.1.0"
