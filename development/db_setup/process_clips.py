import io, os, csv, json, sys, re
import operator, argparse
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import kso_utils.db_utils as db_utils
from kso_utils.zooniverse_utils import auth_session


def main():

    "Handles argument parsing and launches the correct function."
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user", "-u", help="Zooniverse username", type=str, required=True
    )
    parser.add_argument(
        "--password", "-p", help="Zooniverse password", type=str, required=True
    )
    parser.add_argument(
        "-db",
        "--db_path",
        type=str,
        help="the absolute path to the database file",
        default=r"koster_lab.db",
        required=True,
    )    
    parser.add_argument(
        "-zw",
        "--zoo_workflow",
        type=float,
        help="Number of the Zooniverse workflow of interest",
        default=11767,
        required=False,
    )
    parser.add_argument(
        "-zwv",
        "--zoo_workflow_version",
        type=float,
        help="Version number of the Zooniverse workflow of interest",
        default=227,
        required=False,
    )
    parser.add_argument(
        "-thr",
        "--aggr_thresh",
        type=float,
        help="Agreement threshold required among different users",
        default=0.8,
        required=False,
    )
    parser.add_argument(
        "-nu",
        "--n_users",
        type=float,
        help="Minimum number of different Zooniverse users required per clip",
        default=3,
        required=False,
    )
    parser.add_argument(
        "-du",
        "--duplicates_file_id",
        help="Google drive id of list of duplicated subjects",
        type=str,
        required=False,
    )
    
    args = parser.parse_args()

    # Connect to the Zooniverse project
    project = auth_session(args.user, args.password)

    # Get the classifications from the project
    export = project.get_export("classifications")

    # Save the response as pandas data frame
    class_df = pd.read_csv(
        io.StringIO(export.content.decode("utf-8")),
        usecols=[
            "subject_ids",
            "subject_data",
            "classification_id",
            "workflow_id",
            "workflow_version",
            "annotations",
        ],
    )

    
    # Filter clip classifications
    class_df = class_df[
        (class_df.workflow_id == args.zoo_workflow)
        & (class_df.workflow_version >= args.zoo_workflow_version)
    ].reset_index()

        ## Check if subjects have been uploaded
    # Get species id for each species
    conn = db_utils.create_connection(args.db_path)

    # Get subject table
    uploaded_subjects = pd.read_sql_query(
        "SELECT id FROM subjects WHERE subject_type='clip'", conn
    )

    # Add frame subjects to db that have not been uploaded
    new_subjects = class_df[(~class_df.subject_ids.isin(uploaded_subjects))]
    new_subjects["subject_dict"] = new_subjects["subject_data"].apply(lambda x: [v["retired"] for k,v in json.loads(x).items()][0])
    new_subjects = new_subjects[~new_subjects['subject_dict'].isnull()].drop("subject_dict", 1)

    if len(new_subjects) > 0 and args.zoo_workflow not in [11767]:

        # Get info of subjects uploaded to the project
        export = project.get_export("subjects")

        # Save the subjects info as pandas data frame
        subjects_df = pd.read_csv(
            io.StringIO(export.content.decode("utf-8")),
            usecols=["subject_id", "subject_set_id", "created_at"],
        )

        new_subjects = pd.merge(
            new_subjects,
            subjects_df,
            how="left",
            left_on="subject_ids",
            right_on="subject_id",
        )

        # Get movies table
        movies_df = pd.read_sql_query(
        "SELECT id, filename FROM movies", conn
        )

        # Extract the video filename and annotation details
        new_subjects[
            [
                "clip_start_time",
                "clip_end_time", 
                "movie_id",
                "classifications_count",
                "created_at",
                "retired_at",
                "retirement_reason",
            ]
        ] = pd.DataFrame(
            new_subjects["subject_data"]
            .apply(
                lambda x: [
                    {
                        "clip_start_time": v["clip_start_time"] if "clip_start_time" in v else (v["X.start_time"] if "X.start_time" in v else v["#start_time"]),
                        "clip_end_time": v["clip_end_time"] if "clip_end_time" in v else (v["X.end_time"] if "X.end_time" in v else v["#end_time"]),
                        "movie_id": v["movie_id"] if "movie_id" in v else movies_df[movies_df.filename == v["filename"].rsplit('_', 1)[0]]['id'].values[0],
                        "classifications_count": v["retired"]["classifications_count"],
                        "created_at": v["retired"]["created_at"],
                        "retired_at": v["retired"]["retired_at"],
                        "retirement_reason": v["retired"]["retirement_reason"],
                    }
                    for k, v in json.loads(x).items()
                ][0] if len(json.loads(x).items()) > 0 else {}
            )
            .tolist()
        )

        new_subjects["subject_type"] = "clip"
        movies_df = pd.read_sql_query("SELECT id, filename FROM movies", conn)
        movies_df = movies_df.rename(
            columns={"id": "movie_id", "filename": "movie_filename"}
        )
        new_subjects = pd.merge(new_subjects, movies_df, how="left", on="movie_id")

        new_subjects["filename"] = new_subjects.apply(
            lambda x: x["movie_filename"] + "_" + str(x["clip_start_time"]) + ".mp4",
            axis=1,
        )
        # Set irrelevant columns to None
        new_subjects["frame_exp_sp_id"] = None
        new_subjects["frame_number"] = None
        new_subjects = new_subjects[
            [
                "subject_ids",
                "subject_type",
                "filename",
                "clip_start_time",
                "clip_end_time",
                "frame_exp_sp_id",
                "frame_number",
                "workflow_id",
                "subject_set_id",
                "classifications_count",
                "retired_at",
                "retirement_reason",
                "created_at",
                "movie_id",
            ]
        ]
        new_subjects = new_subjects.drop_duplicates(subset="subject_ids")
        db_utils.test_table(new_subjects, "subjects", keys=["movie_id"])

        # Add values to subjects
        db_utils.add_to_table(
            args.db_path, "subjects", [tuple(i) for i in new_subjects.values], 14
        )
    
    # Drop worflow columns
    class_df = class_df.drop(columns=["workflow_id", "workflow_version"])

    
    # Create an empty list
    rows_list = []

    # Loop through each classification submitted by the users
    for index, row in class_df.iterrows():
        # Load annotations as json format
        annotations = json.loads(row["annotations"])

        # Select the information from the species identification task
        for ann_i in annotations:
            if ann_i["task"] == "T4":

                # Select each species annotated and flatten the relevant answers
                for value_i in ann_i["value"]:
                    choice_i = {}
                    # If choice = 'nothing here', set follow-up answers to blank
                    if value_i["choice"] == "NOTHINGHERE":
                        f_time = ""
                        inds = ""
                    # If choice = species, flatten follow-up answers
                    else:
                        answers = value_i["answers"]
                        for k in answers.keys():
                            if "FIRSTTIME" in k:
                                f_time = answers[k].replace("S", "")
                            if "INDIVIDUAL" in k:
                                inds = answers[k]

                    # Save the species of choice, class and subject id
                    choice_i.update(
                        {
                            "classification_id": row["classification_id"],
                            "label": value_i["choice"],
                            "first_seen": f_time,
                            "how_many": inds,
                        }
                    )

                    rows_list.append(choice_i)

    # Create a data frame with annotations as rows
    annot_df = pd.DataFrame(
        rows_list, columns=["classification_id", "label", "first_seen", "how_many"]
    )

    # Specify the type of columns of the df
    annot_df["how_many"] = pd.to_numeric(annot_df["how_many"])
    annot_df["first_seen"] = pd.to_numeric(annot_df["first_seen"])

    # Add subject id to each annotation
    annot_df = pd.merge(
        annot_df,
        class_df.drop(columns=["annotations"]),
        how="left",
        on="classification_id",
    )

    # Clear duplicated subjects
    if args.duplicates_file_id:
        annot_df = db_utils.combine_duplicates(annot_df, args.duplicates_file_id)
        
    # Calculate the number of users that classified each subject
    annot_df["n_users"] = annot_df.groupby("subject_ids")[
        "classification_id"
    ].transform("nunique")

    # Select subjects with at least n different user classifications
    annot_df = annot_df[annot_df.n_users >= args.n_users]

    # Calculate the proportion of users that agreed on their annotations
    annot_df["class_n"] = annot_df.groupby(["subject_ids", "label"])[
        "classification_id"
    ].transform("count")
    annot_df["class_prop"] = annot_df.class_n / annot_df.n_users

    # Select annotations based on agreement threshold
    annot_df = annot_df[annot_df.class_prop >= args.aggr_thresh]

    # Extract the median of the second where the animal/object is and number of animals
    annot_df = annot_df.groupby(["subject_ids", "label"], as_index=False)
    annot_df = pd.DataFrame(annot_df[["how_many", "first_seen"]].median())

    # Create connection to db
    conn = db_utils.create_connection(args.db_path)

    # Retrieve the id and label from the species table
    speciesdf = pd.read_sql_query("SELECT id, label FROM species", conn)
    speciesdf = speciesdf.rename(columns={"id": "species_id"})

    # Match the label format of speciesdf to the annot_df
    speciesdf["label"] = speciesdf["label"].apply(
        lambda x: re.sub(r"[()\s]", "", x).upper(), 1
    )

    # Add species_id to the classifications dataframe
    annot_df = pd.merge(
        annot_df, speciesdf, how="left", on="label", validate="many_to_one"
    )

    # Add index as id and rename the subject_id field
    annot_df = annot_df.reset_index().rename(
        columns={"index": "id", "subject_ids": "subject_id"}
    )

    # Set the columns in the right order
    annot_df = annot_df[["species_id", "how_many", "first_seen", "subject_id"]]

    clipsdf = pd.read_sql_query("SELECT id FROM subjects", conn)

    # Check that the subject exists
    annot_df = annot_df[annot_df["subject_id"].isin(clipsdf["id"].unique())]

    # Add annotations to the agg_annotations_clip table
    db_utils.add_to_table(
        args.db_path, "agg_annotations_clip", [(None,) + tuple(i) for i in annot_df.values], 5
    )


if __name__ == "__main__":
    main()
