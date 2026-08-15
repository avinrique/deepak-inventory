"""SqlCategoryRepository/SqlBrandRepository/SqlUnitRepository against a
live PostgreSQL database. Proves the per-organization unique-name (and, for
Unit, unique-abbreviation) database constraints actually reject a duplicate
with a clean domain exception (DuplicateCategoryNameError/
DuplicateBrandNameError/DuplicateUnitError) rather than a raw
IntegrityError leaking out of the repository — this was previously
untested (and unhandled): see app/repositories/sql/{category,brand,
unit}_repository.py.

Uses the ``live_db`` fixture (tests/conftest.py) — gated on
INVENTORY_TEST_DATABASE_URL, never the app's real database.
"""
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import DuplicateBrandNameError, DuplicateCategoryNameError, DuplicateUnitError
from app.database.session import get_session
from app.models import Organization
from app.repositories.sql.brand_repository import SqlBrandRepository
from app.repositories.sql.category_repository import SqlCategoryRepository
from app.repositories.sql.unit_repository import SqlUnitRepository
from app.schemas.product import BrandCreate, CategoryCreate, UnitCreate


@pytest.fixture()
def org_id(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        session.add(org)
        session.flush()
        return org.id


@pytest.fixture()
def other_org_id(live_db):
    with get_session() as session:
        org = Organization(name="Other Org")
        session.add(org)
        session.flush()
        return org.id


# -- Category ----------------------------------------------------------#

def test_category_create_then_list_round_trips(org_id):
    repo = SqlCategoryRepository()
    created = repo.create(org_id, CategoryCreate(name="Beverages", description="Drinks"))

    listed = repo.list_all(org_id)

    assert created.name == "Beverages"
    assert [c.name for c in listed] == ["Beverages"]


def test_duplicate_category_name_within_same_org_rejected(org_id):
    repo = SqlCategoryRepository()
    repo.create(org_id, CategoryCreate(name="Beverages"))

    with pytest.raises(DuplicateCategoryNameError):
        repo.create(org_id, CategoryCreate(name="Beverages"))


def test_same_category_name_allowed_across_different_orgs(org_id, other_org_id):
    repo = SqlCategoryRepository()
    repo.create(org_id, CategoryCreate(name="Beverages"))

    created = repo.create(other_org_id, CategoryCreate(name="Beverages"))  # must not raise

    assert created.name == "Beverages"


def test_renaming_a_category_to_another_categorys_name_is_rejected(org_id):
    repo = SqlCategoryRepository()
    repo.create(org_id, CategoryCreate(name="Beverages"))
    snacks = repo.create(org_id, CategoryCreate(name="Snacks"))

    with pytest.raises(DuplicateCategoryNameError):
        repo.update(org_id, snacks.id, CategoryCreate(name="Beverages"))


def test_renaming_a_category_to_its_own_current_name_is_allowed(org_id):
    repo = SqlCategoryRepository()
    beverages = repo.create(org_id, CategoryCreate(name="Beverages", description="v1"))

    result = repo.update(org_id, beverages.id, CategoryCreate(name="Beverages", description="v2"))

    assert result.description == "v2"


# -- Brand ---------------------------------------------------------------#

def test_duplicate_brand_name_within_same_org_rejected(org_id):
    repo = SqlBrandRepository()
    repo.create(org_id, BrandCreate(name="Acme Brand"))

    with pytest.raises(DuplicateBrandNameError):
        repo.create(org_id, BrandCreate(name="Acme Brand"))


def test_same_brand_name_allowed_across_different_orgs(org_id, other_org_id):
    repo = SqlBrandRepository()
    repo.create(org_id, BrandCreate(name="Acme Brand"))

    created = repo.create(other_org_id, BrandCreate(name="Acme Brand"))  # must not raise
    assert created.name == "Acme Brand"


# -- Unit ------------------------------------------------------------------#

def test_duplicate_unit_name_within_same_org_rejected(org_id):
    repo = SqlUnitRepository()
    repo.create(org_id, UnitCreate(name="Kilogram", abbreviation="kg"))

    with pytest.raises(DuplicateUnitError):
        repo.create(org_id, UnitCreate(name="Kilogram", abbreviation="kg2"))


def test_duplicate_unit_abbreviation_within_same_org_rejected(org_id):
    repo = SqlUnitRepository()
    repo.create(org_id, UnitCreate(name="Kilogram", abbreviation="kg"))

    with pytest.raises(DuplicateUnitError):
        repo.create(org_id, UnitCreate(name="Kilograms (alt)", abbreviation="kg"))


def test_same_unit_allowed_across_different_orgs(org_id, other_org_id):
    repo = SqlUnitRepository()
    repo.create(org_id, UnitCreate(name="Kilogram", abbreviation="kg"))

    created = repo.create(other_org_id, UnitCreate(name="Kilogram", abbreviation="kg"))
    assert created.abbreviation == "kg"


def test_renaming_a_unit_abbreviation_to_a_taken_one_is_rejected(org_id):
    repo = SqlUnitRepository()
    repo.create(org_id, UnitCreate(name="Kilogram", abbreviation="kg"))
    piece = repo.create(org_id, UnitCreate(name="Piece", abbreviation="pc"))

    with pytest.raises(DuplicateUnitError):
        repo.update(org_id, piece.id, UnitCreate(name="Piece", abbreviation="kg"))


# -- CHECK-constraint violations must never be mislabeled as duplicates ---#
# (regression coverage: an earlier version of this fix blindly treated
# every IntegrityError as "name already exists", which would have
# misreported a blank-name rejection as a duplicate-name one.)

def test_blank_category_name_raises_integrity_error_not_duplicate(org_id):
    # pytest.raises(IntegrityError) only matches IntegrityError itself (or
    # a subclass) — DuplicateCategoryNameError is a sibling AppError, not
    # a subclass, so this fails loudly (not silently) if the mislabeling
    # bug ever comes back.
    repo = SqlCategoryRepository()
    with pytest.raises(IntegrityError):
        repo.create(org_id, CategoryCreate(name="   "))


def test_blank_brand_name_raises_integrity_error_not_duplicate(org_id):
    repo = SqlBrandRepository()
    with pytest.raises(IntegrityError):
        repo.create(org_id, BrandCreate(name=""))


def test_blank_unit_abbreviation_raises_integrity_error_not_duplicate(org_id):
    repo = SqlUnitRepository()
    with pytest.raises(IntegrityError):
        repo.create(org_id, UnitCreate(name="Kilogram", abbreviation=""))
